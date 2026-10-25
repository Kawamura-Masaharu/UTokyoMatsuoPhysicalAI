#!/usr/bin/env python3
"""Summarize saved navigation events without calling an API or importing ROS.

Usage: python3 tools/summarize_log.py evidence/gemini_nav_20261025.jsonl
The four evaluation inputs below have expectations specified independently
of the recorded model responses. Unknown inputs are summarized but not scored.
Elapsed times use the application's monotonic counters, not UTC differences.
"""

import argparse
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import sys


EXPECTED = {
    "受付に行ってから倉庫へ行って": ("順次訪問", "ready", ["A", "B"]),
    "倉庫に行って戻って": ("曖昧な戻り先", "clarify", []),
    "受付、検査室、倉庫の順に回って": ("未登録地点", "clarify", []),
    "受付、倉庫、受付の順に回って": ("再訪を含む訪問", "ready", ["A", "B", "A"]),
}


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_events(path):
    events = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_constant=_reject_constant)
            except ValueError as error:
                raise ValueError(f"Invalid JSON at line {number}") from error
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("run_id"), str)
                or not row["run_id"]
                or not isinstance(row.get("event"), str)
            ):
                raise ValueError(f"Missing run_id/event at line {number}")
            events.append(row)
    if not events:
        raise ValueError("The log has no events")
    return events


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


def stats(values):
    values = [v for v in values if _number(v) is not None]
    if not values:
        return {"n": 0, "min": None, "max": None, "mean": None}
    return {
        "n": len(values), "min": min(values), "max": max(values),
        "mean": round(statistics.mean(values), 6),
    }


def summarize(events, source_name):
    groups = OrderedDict()
    for row in events:
        groups.setdefault(row["run_id"], []).append(row)
    runs, issues = [], []
    for run_id, rows in groups.items():
        event_names = [r["event"] for r in rows]
        selected = {name: [r for r in rows if r["event"] == name]
                    for name in ("start", "plan", "finish", "goal_start", "goal_result")}
        for name in ("start", "finish"):
            if len(selected[name]) != 1:
                issues.append(f"{run_id}: expected exactly one {name}")
        if event_names[0] != "start" or event_names[-1] != "finish":
            issues.append(f"{run_id}: incomplete event sequence")
        counters = [_number(r.get("elapsed_seconds")) for r in rows]
        if any(v is None for v in counters) or any(b < a for a, b in zip(counters, counters[1:])
                                                   if a is not None and b is not None):
            issues.append(f"{run_id}: invalid monotonic elapsed counters")
        start = selected["start"][0] if selected["start"] else {}
        finish = selected["finish"][-1] if selected["finish"] else {}
        plan_event = selected["plan"][0] if selected["plan"] else {}
        plan = plan_event.get("plan", {})
        if not isinstance(plan, dict):
            plan = {}
            issues.append(f"{run_id}: plan is not an object")
        outcome = finish.get("outcome")
        if outcome in ("succeeded", "clarify", "plan_only") and len(selected["plan"]) != 1:
            issues.append(f"{run_id}: expected exactly one adopted plan")
        mode = start.get("plan_only")
        if not isinstance(mode, bool):
            issues.append(f"{run_id}: plan_only must be a boolean")
        instruction = start.get("instruction")
        if not isinstance(instruction, str):
            instruction = ""
            issues.append(f"{run_id}: missing instruction")
        expected = EXPECTED.get(instruction)
        matches = None
        goal_starts, goal_results = selected["goal_start"], selected["goal_result"]
        if expected:
            _, expected_status, expected_route = expected
            expected_outcome = "clarify" if expected_status == "clarify" else (
                "plan_only" if mode else "succeeded")
            executed_route = expected_route if expected_outcome == "succeeded" else []
            expected_events = ["start", "plan"] + [
                name for _ in executed_route for name in ("goal_start", "goal_result")
            ] + ["finish"]
            matches = (
                plan.get("status") == expected_status
                and plan.get("route") == expected_route
                and outcome == expected_outcome
                and event_names == expected_events
                and [r.get("goal_id") for r in goal_starts] == executed_route
                and [r.get("goal_id") for r in goal_results] == executed_route
                and [r.get("goal_index") for r in goal_starts] == list(range(1, len(executed_route) + 1))
                and [r.get("goal_index") for r in goal_results] == list(range(1, len(executed_route) + 1))
                and all(r.get("result") == "SUCCEEDED" for r in goal_results)
            )
        timestamp = start.get("timestamp")
        try:
            instant = datetime.fromisoformat(timestamp)
            if instant.tzinfo is None:
                raise ValueError("Missing time zone")
            started_jst = instant.astimezone(timezone(timedelta(hours=9))).isoformat()
        except (TypeError, ValueError):
            started_jst = None
            issues.append(f"{run_id}: invalid timestamp")
        usage = plan_event.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        runs.append({
            "run_id": run_id,
            "started_at_jst": started_jst,
            "instruction": instruction,
            "plan_only": mode,
            "model": plan_event.get("model", start.get("model")),
            "plan_status": plan.get("status"),
            "route": plan.get("route"),
            "outcome": outcome,
            "matches_expected": matches,
            "api_latency_seconds": _number(plan_event.get("latency_seconds")),
            "run_elapsed_seconds": _number(finish.get("elapsed_seconds")),
            "accepted_goal_count": len(goal_starts),
            "goal_results": [{
                "goal_id": r.get("goal_id"), "goal_index": r.get("goal_index"),
                "result": r.get("result"),
                "goal_elapsed_seconds": _number(r.get("goal_elapsed_s")),
            } for r in goal_results],
            "reported_total_tokens": _number(usage.get("totalTokenCount")),
        })
    execution = [r for r in runs if r["plan_only"] is False]
    cases = []
    for instruction, (label, status, route) in EXPECTED.items():
        trials = [r for r in execution if r["instruction"] == instruction]
        cases.append({
            "case": label, "instruction": instruction,
            "expected_status": status, "expected_route": route,
            "trials": len(trials),
            "matching_trials": sum(r["matches_expected"] is True for r in trials),
            "run_ids": [r["run_id"] for r in trials],
            "outcome_counts": dict(Counter(r["outcome"] for r in trials)),
            "run_elapsed_seconds": [r["run_elapsed_seconds"] for r in trials],
            "api_latency_seconds": [r["api_latency_seconds"] for r in trials],
        })
    return {
        "source_log": source_name,
        "record_count": len(events), "run_count": len(runs),
        "plan_only_count": sum(r["plan_only"] is True for r in runs),
        "execution_mode_count": len(execution),
        "outcome_counts": dict(Counter(r["outcome"] for r in runs)),
        "accepted_goal_count": sum(r["accepted_goal_count"] for r in runs),
        "succeeded_goal_count": sum(g["result"] == "SUCCEEDED" for r in runs for g in r["goal_results"]),
        "clarify_goal_start_count": sum(r["accepted_goal_count"] for r in runs if r["outcome"] == "clarify"),
        "api_latency_seconds": stats([r["api_latency_seconds"] for r in runs]),
        "reported_total_tokens": sum(r["reported_total_tokens"] or 0 for r in runs),
        "timing_definitions": {
            "api_latency_seconds": "Client monotonic time: HTTP request through response parsing and plan validation.",
            "run_elapsed_seconds": "finish.elapsed_seconds: API, ROS setup/readiness, navigation and cleanup are included.",
            "goal_elapsed_seconds": "Goal submission start through observation of the terminal Nav2 result; acceptance wait is included.",
            "note": "UTC timestamps are for ordering and dates; do not use their differences for these durations.",
        },
        "cases": cases, "runs": runs, "issues": issues,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = summarize(read_events(args.log_file), args.log_file.name)
    except (OSError, ValueError) as error:
        print(f"Log summary failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if result["issues"] or any(r["matches_expected"] is False for r in result["runs"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
