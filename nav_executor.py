"""Execute validated A/B routes with an already running ROS 2 Humble Nav2.

Derived from physical_ai_nav/nav_roundtrip.py, whose A -> B movement was
successfully verified in the lecture Docker environment. This module neither
publishes an initial pose nor changes the lifecycle state of the Nav2 servers.

Import this module only when movement is requested: it depends on the ROS apt
Python environment. The caller owns the CLI, LLM requests, and event log.
BasicNavigator has internal blocking waits in goToPose() and cancelTask(); the
outer monotonic timeouts below are not hard deadlines for those calls.
"""

import math
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Callable

import yaml
import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.parameter import Parameter
from rclpy.signals import SignalHandlerOptions


def load_pose(path):
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    position = [float(data["position"][axis]) for axis in ("x", "y", "z")]
    orientation = [
        float(data["orientation"][axis]) for axis in ("x", "y", "z", "w")
    ]
    if not all(math.isfinite(value) for value in position + orientation):
        raise ValueError(f"{path}: position/orientation must be finite")
    norm = math.sqrt(sum(value * value for value in orientation))
    if not math.isfinite(norm) or norm < 1e-9:
        raise ValueError(f"{path}: invalid quaternion")
    return position, [value / norm for value in orientation]


def make_goal(navigator, saved_pose):
    position, orientation = saved_pose
    goal = PoseStamped()
    goal.header.frame_id = "map"
    goal.header.stamp = navigator.get_clock().now().to_msg()
    for axis, value in zip(("x", "y", "z"), position):
        setattr(goal.pose.position, axis, value)
    for axis, value in zip(("x", "y", "z", "w"), orientation):
        setattr(goal.pose.orientation, axis, value)
    return goal


def check_stop(stop_requested):
    if stop_requested.is_set():
        raise InterruptedError("Ctrl+C によるキャンセル要求")


def wait_ready(navigator, stop_requested, timeout=30.0):
    """Receive existing AMCL state and /clock without resetting localization."""
    deadline = time.monotonic() + timeout
    print("起動済みの Nav2、AMCL、/clock を確認中...", flush=True)
    while rclpy.ok():
        check_stop(stop_requested)
        rclpy.spin_once(navigator, timeout_sec=0.1)
        if (
            navigator.initial_pose_received
            and navigator.get_clock().now().nanoseconds > 0
            and navigator.nav_to_pose_client.server_is_ready()
        ):
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Nav2/AMCL/シミュレーション時刻を確認できない。"
                "Gazebo と Nav2 を起動し、RViz の初期位置を確認すること。"
            )
    raise RuntimeError("ROS context が終了した")


def _cleanup_message(message):
    # A broken output stream must not prevent cancellation or hide its cause.
    try:
        print(message, file=sys.stderr, flush=True)
    except BaseException:
        pass


def _cancel_and_wait(navigator, emit):
    """Best-effort cancellation that preserves the exception being unwound."""
    def safe_emit(event_name, **fields):
        # In normal execution log failures abort the route. During cancellation,
        # recording a failure must never prevent the actual cancel request.
        try:
            emit(event_name, **fields)
        except BaseException as error:
            _cleanup_message(f"キャンセル中のログ記録に失敗: {error}")

    def unconfirmed(reason):
        safe_emit("cancel_unconfirmed", reason=reason)
        _cleanup_message(
            "キャンセル要求後のタスク終了を確認できなかった。"
            f"Gazebo で動作を確認すること。理由: {reason}"
        )

    safe_emit("cancel_requested")
    _cleanup_message("現在の目標へキャンセルを要求する...")
    try:
        if not rclpy.ok():
            unconfirmed("ROS context が終了しているため要求を送信できない")
            return
        navigator.cancelTask()
        deadline = time.monotonic() + 10.0
        while rclpy.ok():
            if navigator.isTaskComplete():
                result = navigator.getResult()
                safe_emit("goal_result", result=result.name)
                # A goal can finish successfully while cancellation is in flight.
                safe_emit(
                    "cancel_observed",
                    result=result.name,
                    canceled=(result == TaskResult.CANCELED),
                )
                _cleanup_message(
                    f"タスク終了: {result.name}。次の目標は送信しない。"
                )
                return
            if time.monotonic() >= deadline:
                unconfirmed("キャンセル応答後 10 秒以内にタスク終了を確認できない")
                return
        unconfirmed("キャンセル待機中に ROS context が終了した")
    except BaseException as error:
        unconfirmed(f"{type(error).__name__}: {error}")


def _run(navigator, poses, stop_requested, leg_timeout, log_event):
    started = time.monotonic()
    active_goal = False
    goal_id = None
    goal_index = 0
    goal_started = started

    def emit(event_name, **fields):
        now = time.monotonic()
        log_event(
            event_name,
            goal_id=goal_id,
            goal_index=goal_index,
            elapsed_s=round(now - started, 6),
            goal_elapsed_s=round(now - goal_started, 6),
            **fields,
        )

    try:
        wait_ready(navigator, stop_requested)
        for goal_index, (goal_id, saved_pose) in enumerate(poses, start=1):
            check_stop(stop_requested)
            print(f"\n{goal_id} 地点へ移動する ({goal_index}/{len(poses)})", flush=True)
            goal = make_goal(navigator, saved_pose)
            goal_started = time.monotonic()
            check_stop(stop_requested)
            if not navigator.goToPose(goal):
                emit("goal_result", result="REJECTED")
                check_stop(stop_requested)
                raise RuntimeError(f"{goal_id}: Nav2 が目標を受理しなかった")
            # Set this before invoking any user-supplied callback: logging can
            # fail after Nav2 has already accepted and started the goal.
            active_goal = True
            deadline = time.monotonic() + leg_timeout
            emit("goal_start")
            check_stop(stop_requested)
            next_feedback = 0.0

            while rclpy.ok():
                check_stop(stop_requested)
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"{goal_id}: {leg_timeout:g} 秒の制限時間を超過"
                    )
                if navigator.isTaskComplete():
                    break
                if time.monotonic() >= next_feedback:
                    feedback = navigator.getFeedback()
                    if feedback is not None:
                        print(
                            f"{goal_id}: 残り距離 {feedback.distance_remaining:.2f} m",
                            flush=True,
                        )
                    next_feedback = time.monotonic() + 2.0
            else:
                raise RuntimeError("移動中に ROS context が終了した")

            active_goal = False
            result = navigator.getResult()
            emit("goal_result", result=result.name)
            print(f"{goal_id}: {result.name}", flush=True)
            check_stop(stop_requested)
            if result != TaskResult.SUCCEEDED:
                raise RuntimeError(f"{goal_id} に到達できなかったため移動を終了")
        check_stop(stop_requested)
        print("\n移動完了: " + " → ".join(name for name, _ in poses), flush=True)
    finally:
        if active_goal:
            _cancel_and_wait(navigator, emit)


def execute_route(
    route: list[str],
    poses_dir: Path,
    leg_timeout: float,
    log_event: Callable[..., None],
) -> None:
    """Visit a nonempty route of up to eight A/B IDs; raise on any failure.

    Call from the CLI's main thread with no existing rclpy context. All pose
    files are loaded before ROS starts. Ctrl+C requests cancellation and raises
    InterruptedError after the cancellation attempt; the caller handles exit
    status 130. A failed/canceled leg never permits a later goal to be sent.

    goal_start denotes an accepted goal. goal_result carries its Nav2 result
    name (or REJECTED when not accepted). cancel_observed means a terminal
    result was observed; its canceled field distinguishes CANCELED from a
    competing terminal result. Cancellation logs are best effort, since a
    failed logging callback must not obstruct cancellation.
    """
    if (
        not isinstance(route, list)
        or not 1 <= len(route) <= 8
        or any(not isinstance(name, str) or name not in ("A", "B") for name in route)
    ):
        raise ValueError("route は A/B の地点 ID を 1～8 個並べたリストにすること")
    if not math.isfinite(leg_timeout) or leg_timeout <= 0:
        raise ValueError("leg_timeout は有限の正数にすること")
    if not callable(log_event):
        raise TypeError("log_event は呼び出し可能な関数にすること")
    route = list(route)
    poses_dir = Path(poses_dir)
    saved = {
        name: load_pose(poses_dir / f"point_{name.lower()}.yaml")
        for name in dict.fromkeys(route)
    }
    poses = [(name, saved[name]) for name in route]

    stop_requested = threading.Event()
    previous_sigint = signal.getsignal(signal.SIGINT)
    navigator = None
    initialized = False
    try:
        rclpy.init(args=[], signal_handler_options=SignalHandlerOptions.NO)
        initialized = True
        signal.signal(signal.SIGINT, lambda _signum, _frame: stop_requested.set())
        navigator = BasicNavigator(node_name="natural_language_navigator")
        results = navigator.set_parameters(
            [Parameter("use_sim_time", Parameter.Type.BOOL, True)]
        )
        if not all(result.successful for result in results):
            raise RuntimeError("use_sim_time=True を設定できなかった")
        _run(navigator, poses, stop_requested, leg_timeout, log_event)
    finally:
        # Preserve the primary exception (especially InterruptedError) while
        # still attempting every cleanup step and restoring the prior handler.
        pending_error = sys.exc_info()[0] is not None
        cleanup_error = None
        for cleanup in (
            lambda: navigator.destroy_node() if navigator is not None else None,
            lambda: rclpy.shutdown() if initialized and rclpy.ok() else None,
            lambda: signal.signal(signal.SIGINT, previous_sigint),
        ):
            try:
                cleanup()
            except BaseException as error:
                _cleanup_message(f"ROS 終了処理に失敗: {error}")
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None and not pending_error:
            raise cleanup_error
