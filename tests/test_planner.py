"""Offline boundary tests using fabricated Gemini responses and mocked execution.

No request reaches Gemini and no ROS module is needed. These tests check the
application's boundaries, not the model's Japanese instruction understanding.
Run the manual examples in README.md separately for a live evaluation.
"""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nav_gemini as app


TEST_MODEL = "gemini-3.5-flash-lite"
TEST_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + TEST_MODEL
    + ":generateContent"
)


def plan(route=None, status="ready", message="受付から倉庫へ移動する。"):
    return {
        "status": status,
        "route": ["A", "B"] if route is None else route,
        "message": message,
    }


def response_for(value):
    return {
        "responseId": "response_offline_fixture",
        "modelVersion": TEST_MODEL,
        "candidates": [
            {
                "index": 0,
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps(value, ensure_ascii=False)}],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 6,
            "totalTokenCount": 18,
        },
    }


class PlanValidationTests(unittest.TestCase):
    def test_ready_preserves_visit_order_and_repetition(self):
        expected = plan(["B", "A", "B", "A"])
        self.assertEqual(app.validate_plan(expected), expected)

    def test_clarify_accepts_no_partial_route(self):
        expected = plan([], "clarify", "倉庫の後はどこへ戻るか。")
        self.assertEqual(app.validate_plan(expected), expected)

    def test_rejects_missing_extra_and_non_object_fields(self):
        invalid = [
            None,
            [],
            "A",
            {"status": "ready", "route": ["A"]},
            {**plan(), "coordinates": [0, 0]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(app.PlanningError):
                app.validate_plan(value)

    def test_rejects_unknown_ids_and_invalid_route_types(self):
        for route in (["C"], ["a"], [1], [True], [None], [["A"]], "A", None):
            value = {**plan(), "route": route}
            with self.subTest(route=route), self.assertRaises(app.PlanningError):
                app.validate_plan(value)

    def test_visit_limit_is_checked_without_truncation(self):
        route = ["A", "B"] * 4
        self.assertEqual(app.validate_plan(plan(route))["route"], route)
        with self.assertRaises(app.PlanningError):
            app.validate_plan(plan(["A"] * 9))

    def test_rejects_inconsistent_status_and_route(self):
        for value in (plan([], "ready"), plan(["A"], "clarify"), plan(["A"], "unknown")):
            with self.subTest(value=value), self.assertRaises(app.PlanningError):
                app.validate_plan(value)

    def test_rejects_empty_long_and_control_character_messages(self):
        for message in (None, 1, "", "   ", "長" * 401, "確認\n質問", "\x1b[2J", "\x7f"):
            with self.subTest(message=repr(message)), self.assertRaises(app.PlanningError):
                app.validate_plan(plan(message=message))


class ResponseParsingTests(unittest.TestCase):
    def test_accepts_completed_plan_with_model_or_absent_role(self):
        expected = plan(["A", "B", "A"])
        for omit_role in (False, True):
            value = response_for(expected)
            if omit_role:
                del value["candidates"][0]["content"]["role"]
            with self.subTest(omit_role=omit_role):
                self.assertEqual(app.parse_response(value), expected)

    def test_thought_text_is_not_parsed_as_the_final_plan(self):
        expected = plan(["B", "A"])
        value = response_for(expected)
        value["candidates"][0]["content"]["parts"] = [
            {"thought": True, "text": "Offline thought text; this is deliberately not JSON."},
            {
                "thought": False,
                "thoughtSignature": "opaque-offline-signature",
                "text": json.dumps(expected, ensure_ascii=False),
            },
        ]
        self.assertEqual(app.parse_response(value), expected)

    def test_rejects_non_stop_finish_reasons_even_with_valid_json(self):
        reasons = ("MAX_TOKENS", "SAFETY", "RECITATION", "OTHER", "FINISH_REASON_UNSPECIFIED", None)
        for reason in reasons:
            value = response_for(plan())
            value["candidates"][0]["finishReason"] = reason
            with self.subTest(reason=reason), self.assertRaises(app.PlanningError):
                app.parse_response(value)

    def test_rejects_blocked_prompt_even_if_a_candidate_is_present(self):
        for reason in ("SAFETY", "OTHER", "PROHIBITED_CONTENT"):
            value = response_for(plan())
            value["promptFeedback"] = {"blockReason": reason}
            with self.subTest(reason=reason), self.assertRaises(app.PlanningError):
                app.parse_response(value)

    def test_rejects_missing_or_multiple_candidates_and_error_envelopes(self):
        multiple = response_for(plan())
        multiple["candidates"].append(response_for(plan(["B"]))["candidates"][0])
        invalid = (None, {}, {"candidates": []}, multiple, {"error": {"status": "INTERNAL"}})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(app.PlanningError):
                app.parse_response(value)

    def test_rejects_unexpected_role_parts_and_thought_only_output(self):
        invalid_contents = [
            {"role": "user", "parts": [{"text": json.dumps(plan())}]},
            {"role": "model", "parts": []},
            {"role": "model", "parts": [{"thought": True, "text": json.dumps(plan())}]},
            {"role": "model", "parts": [{"functionCall": {"name": "drive", "args": {}}}]},
            {"role": "model", "parts": [{"text": json.dumps(plan()), "unexpected": True}]},
            {"role": "model", "parts": [{"text": json.dumps(plan()), "thought": "true"}]},
        ]
        for content in invalid_contents:
            value = response_for(plan())
            value["candidates"][0]["content"] = content
            with self.subTest(content=content), self.assertRaises(app.PlanningError):
                app.parse_response(value)

    def test_rejects_refusal_text_invalid_json_duplicate_keys_and_unknown_ids(self):
        raw_texts = [
            "I cannot comply with this request.",
            "```json\n{}\n```",
            '{"status":"ready","route":["A"],"route":["B"],"message":"重複"}',
            json.dumps(plan(["C"])),
        ]
        for raw_text in raw_texts:
            value = response_for(plan())
            value["candidates"][0]["content"]["parts"][0]["text"] = raw_text
            with self.subTest(raw_text=raw_text), self.assertRaises(app.PlanningError):
                app.parse_response(value)


class TransportTests(unittest.TestCase):
    def test_posts_generate_content_with_schema_and_header_only_key(self):
        expected = plan(["A", "B", "A"])
        response = response_for(expected)
        fake_key = "offline-test-not-a-real-key"
        with patch.object(app.urllib.request, "build_opener") as build_opener:
            opener = build_opener.return_value
            stream = opener.open.return_value.__enter__.return_value
            stream.read.return_value = json.dumps(response, ensure_ascii=False).encode("utf-8")
            actual, metadata = app.request_plan(" 受付、倉庫、受付へ ", TEST_MODEL, fake_key, 7.5)

        self.assertEqual(actual, expected)
        self.assertEqual(metadata["response_id"], response["responseId"])
        self.assertEqual(metadata["model"], response["modelVersion"])
        self.assertEqual(metadata["usage"], response["usageMetadata"])
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, TEST_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-goog-api-key"), fake_key)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7.5)
        self.assertIsInstance(build_opener.call_args.args[0], app._NoRedirect)
        payload = json.loads(request.data)
        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": app.SYSTEM_INSTRUCTIONS}]})
        self.assertEqual(payload["contents"], [{"role": "user", "parts": [{"text": "受付、倉庫、受付へ"}]}])
        config = payload["generationConfig"]
        self.assertEqual(config["candidateCount"], 1)
        self.assertEqual(config["maxOutputTokens"], 4096)
        self.assertEqual(config["thinkingConfig"], {"thinkingLevel": "LOW", "includeThoughts": False})
        self.assertNotIn("responseMimeType", config)
        self.assertNotIn("responseJsonSchema", config)
        output = config["responseFormat"]["text"]
        self.assertEqual(output["mimeType"], "APPLICATION_JSON")
        self.assertIs(output["schema"]["additionalProperties"], False)
        self.assertEqual(set(output["schema"]["required"]), {"status", "route", "message"})
        self.assertEqual(output["schema"]["properties"]["route"]["items"]["enum"], ["A", "B"])
        self.assertNotIn(fake_key, request.full_url)
        self.assertNotIn(fake_key, request.data.decode("utf-8"))

    def test_authentication_diagnostics_keep_raw_message_and_key_private(self):
        fake_key = "offline-secret-sentinel"
        for http_code, status in ((400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED")):
            body = json.dumps({"error": {
                "code": http_code,
                "status": status,
                "message": f"Raw server message containing {fake_key}",
                "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
            }}).encode()
            failure = urllib.error.HTTPError(TEST_ENDPOINT, http_code, "Failure", {}, io.BytesIO(body))
            with self.subTest(http_code=http_code), patch.object(app.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(app.PlanningError) as caught:
                    app.request_plan("受付へ", TEST_MODEL, fake_key, 1.0)
                diagnostic = str(caught.exception)
                self.assertIn(f"HTTP {http_code}", diagnostic)
                self.assertIn("API_KEY_INVALID", diagnostic)
                self.assertNotIn(fake_key, diagnostic)
                self.assertNotIn("Raw server message", diagnostic)

    def test_rate_limit_diagnostic_includes_only_defined_status_and_retry_time(self):
        body = json.dumps({"error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "offline-private-server-message",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}],
        }}).encode()
        failure = urllib.error.HTTPError(TEST_ENDPOINT, 429, "Too Many Requests", {}, io.BytesIO(body))
        with patch.object(app.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = failure
            with self.assertRaises(app.PlanningError) as caught:
                app.request_plan("受付へ", TEST_MODEL, "offline-key", 1.0)
        diagnostic = str(caught.exception)
        self.assertIn("HTTP 429", diagnostic)
        self.assertIn("RESOURCE_EXHAUSTED", diagnostic)
        self.assertIn("17", diagnostic)
        self.assertNotIn("offline-private-server-message", diagnostic)

    def test_connection_and_timeout_errors_do_not_expose_raw_details(self):
        failures = (urllib.error.URLError("offline-private-detail"), TimeoutError("offline-private-detail"))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(app.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = failure
                with self.assertRaises(app.PlanningError) as caught:
                    app.request_plan("受付へ", TEST_MODEL, "offline-key", 1.0)
                self.assertIn("通信", str(caught.exception))
                self.assertNotIn("offline-private-detail", str(caught.exception))


class ExecutionGateTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            instruction="受付、倉庫、受付の順に回って",
            model=TEST_MODEL,
            plan_only=False,
            poses_dir=Path("/unused/offline/poses"),
            timeout=180.0,
            api_timeout=60.0,
        )
        self.log_event = Mock()
        self.metadata = {"response_id": "response_offline_fixture", "model": TEST_MODEL, "usage": {}, "latency_seconds": 0.0}
        environment = patch.dict(os.environ, {"GEMINI_API_KEY": "offline-test-not-a-key", "GEMINI_MODEL": TEST_MODEL})
        environment.start()
        self.addCleanup(environment.stop)
        stdout = patch("sys.stdout", new_callable=io.StringIO)
        self.stdout = stdout.start()
        self.addCleanup(stdout.stop)

    def test_plan_only_does_not_execute(self):
        self.args.plan_only = True
        with patch.object(app, "request_plan", return_value=(plan(), self.metadata)), \
                patch.object(app, "execute_route") as execute:
            self.assertEqual(app.run_session(self.args, self.log_event), 0)
            execute.assert_not_called()

    def test_clarification_does_not_execute(self):
        clarification = plan([], "clarify", "戻り先を受付か倉庫で指定してほしい。")
        with patch.object(app, "request_plan", return_value=(clarification, self.metadata)), \
                patch.object(app, "execute_route") as execute:
            self.assertEqual(app.run_session(self.args, self.log_event), 3)
            execute.assert_not_called()

    def test_invalid_plan_and_api_error_never_execute(self):
        cases = [
            {"return_value": (plan(["C"]), self.metadata)},
            {"side_effect": app.PlanningError("Offline API failure fixture")},
        ]
        for options in cases:
            with self.subTest(options=options), patch.object(app, "request_plan", **options), \
                    patch.object(app, "execute_route") as execute:
                with self.assertRaises(app.PlanningError):
                    app.run_session(self.args, self.log_event)
                execute.assert_not_called()

    def test_actual_http_failure_never_reaches_execution(self):
        body = json.dumps({"error": {"status": "UNAVAILABLE"}}).encode()
        failure = urllib.error.HTTPError(TEST_ENDPOINT, 503, "Unavailable", {}, io.BytesIO(body))
        with patch.object(app.urllib.request, "build_opener") as build, \
                patch.object(app, "execute_route") as execute:
            build.return_value.open.side_effect = failure
            with self.assertRaises(app.PlanningError):
                app.run_session(self.args, self.log_event)
            execute.assert_not_called()

    def test_ready_executes_exact_order_including_return_visit(self):
        route = ["A", "B", "A"]
        with patch.object(app, "request_plan", return_value=(plan(route), self.metadata)), \
                patch.object(app, "execute_route") as execute:
            self.assertEqual(app.run_session(self.args, self.log_event), 0)
            execute.assert_called_once_with(route, self.args.poses_dir, self.args.timeout, self.log_event)

    def test_main_redacts_key_from_unexpected_error_and_written_log(self):
        fake_key = os.environ["GEMINI_API_KEY"]
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "events.jsonl"
            with patch.object(app, "run_session", side_effect=RuntimeError(f"Unexpected error: {fake_key}")), \
                    patch.object(app, "execute_route") as execute:
                code = app.main(["--plan-only", "--log-file", str(log_path), "受付へ"])
            log_text = log_path.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertNotIn(fake_key, self.stdout.getvalue())
        self.assertNotIn(fake_key, log_text)
        self.assertIn("[REDACTED]", self.stdout.getvalue())
        self.assertIn("[REDACTED]", log_text)


if __name__ == "__main__":
    unittest.main()
