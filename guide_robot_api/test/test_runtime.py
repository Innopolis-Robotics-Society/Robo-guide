"""Real ROS action transport and headless public API with mocked robot subsystems."""

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path

import rclpy
from aiohttp.test_utils import TestClient, TestServer
from guide_robot_api.api_node import RobotApiNode
from guide_robot_api.lib.qos import QOS_MISSION_STATE
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from guide_robot_msgs.action import RunTour, Say
from guide_robot_msgs.msg import MissionState

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/jetson-launcher"))
from guide_launcher.bridge import Bridge, FrameWatcher  # noqa: E402
from guide_launcher.config import Config  # noqa: E402
from guide_launcher.server import create_app  # noqa: E402
from guide_launcher.stack import StackMonitor, StackState  # noqa: E402


def test_real_actions_and_standalone_client_without_ui(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-private-token")
    rclpy.init(args=["--ros-args", "-p", f"bridge_token_file:={token}", "-p", "http_port:=0"])
    adapter = RobotApiNode()
    robot = Node("api_mock_robot")
    from std_msgs.msg import String
    from rclpy.qos import QoSProfile, DurabilityPolicy
    localization_pub = robot.create_publisher(String, "/localization/status",
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    robot.create_timer(.05, lambda: localization_pub.publish(String(
        data='{"state":"ready","motion_blocked":false,"fresh":true,"reset_required":false,"reason":""}')))

    group = ReentrantCallbackGroup()
    stop = threading.Event()
    started = threading.Event()
    counts = {"mission": 0, "speech": 0}

    def mission(goal):
        counts["mission"] += 1
        started.set()
        goal.publish_feedback(RunTour.Feedback(stop_total=1))
        deadline = time.monotonic() + 10
        while not stop.is_set() and not goal.is_cancel_requested and time.monotonic() < deadline:
            time.sleep(0.01)
        if goal.is_cancel_requested:
            goal.canceled()
            return RunTour.Result(outcome=1)
        goal.succeed()
        return RunTour.Result(outcome=0, stops_completed=1)

    def speech(goal):
        counts["speech"] += 1
        goal.publish_feedback(Say.Feedback())
        if goal.request.text == "wait":
            deadline = time.monotonic() + 5
            while not goal.is_cancel_requested and time.monotonic() < deadline:
                time.sleep(0.01)
            if goal.is_cancel_requested:
                goal.canceled()
                return Say.Result(status=2)
        goal.succeed()
        return Say.Result(status=0, spoken_text=goal.request.text)

    actions = [
        ActionServer(
            robot,
            RunTour,
            "run_tour",
            execute_callback=mission,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=group,
        ),
        ActionServer(
            robot,
            Say,
            "say",
            execute_callback=speech,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=group,
        ),
    ]

    def halt(request, response):
        stop.set()
        response.success = True
        return response

    robot.create_service(Trigger, "/mission_fsm/request_stop", halt, callback_group=group)
    publisher = robot.create_publisher(MissionState, "/mission/state", QOS_MISSION_STATE)
    robot.create_timer(0.2, lambda: publisher.publish(MissionState(state=MissionState.STATE_IDLE)))
    supervisor = robot.create_publisher(String, "/supervisor/state", 10)
    robot.create_timer(0.1, lambda: supervisor.publish(String(data="ACTIVE")))
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(adapter)
    executor.add_node(robot)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()

    async def scenario():
        port = adapter._server.runner.addresses[0][1]
        bridge = Bridge(f"http://127.0.0.1:{port}", "test-private-token")
        cfg = Config(
            container="",
            operator_pin="12345678",
            serve_ui=False,
            web_dir="/missing",
            state_dir=str(tmp_path / "state"),
        )
        monitor = StackMonitor(cfg, bridge.probe)
        monitor.state = StackState.UP
        watcher = FrameWatcher(bridge, lambda: True, retry_s=0.05)
        app = create_app(
            cfg, bridge=bridge, monitor=monitor, watcher=watcher, run_background=False
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        watch = asyncio.create_task(watcher.run_forever())
        event = asyncio.create_task(app["api"].events.run())
        try:
            for _ in range(100):
                frame = watcher.latest()
                if (
                    frame
                    and frame.data["readiness"]["mission"]
                    and frame.data["readiness"]["speech"]
                    and frame.data["supervisor_state"] == "ACTIVE"
                ):
                    break
                await asyncio.sleep(0.05)
            assert frame and frame.data["readiness"]["mission"]
            assert frame.data["supervisor_state"] == "ACTIVE"
            # Import the example by path: no frontend module/package dependencies.
            spec = importlib.util.spec_from_file_location(
                "standalone_robot_client", REPO / "examples/api/client.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            external = module.Robot(str(client.make_url("")).rstrip("/"))
            await asyncio.to_thread(external.login, "12345678")
            state = await asyncio.to_thread(external.request, "GET", "/state")
            assert state["data_fresh"]
            assert (await client.get("/")).status == 404
            op = await asyncio.to_thread(
                external.request, "POST", "/missions", {"tour_id": "mock"}, "one"
            )
            assert await asyncio.to_thread(started.wait, 3)
            again = await asyncio.to_thread(
                external.request, "POST", "/missions", {"tour_id": "mock"}, "one"
            )
            assert again["id"] == op["id"] and counts["mission"] == 1
            await asyncio.to_thread(external.request, "POST", "/missions/stop", {})
            for _ in range(80):
                op = await asyncio.to_thread(external.request, "GET", "/operations/" + op["id"])
                if op["status"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            assert op["status"] == "succeeded", op
            spoken = await asyncio.to_thread(
                external.request, "POST", "/speech", {"text": "hello"}, "say"
            )
            for _ in range(80):
                spoken = await asyncio.to_thread(
                    external.request, "GET", "/operations/" + spoken["id"]
                )
                if spoken["status"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            assert spoken["result"]["spoken_text"] == "hello"
            assert counts["speech"] == 1
            pending = await asyncio.to_thread(
                external.request, "POST", "/speech", {"text": "wait"}, "cancel-say"
            )
            for _ in range(60):
                pending = await asyncio.to_thread(
                    external.request, "GET", "/operations/" + pending["id"]
                )
                if pending["status"] == "running":
                    break
                await asyncio.sleep(0.05)
            canceled = await asyncio.to_thread(
                external.request, "POST", "/operations/" + pending["id"] + "/cancel", {}
            )
            assert canceled["accepted"]
            for _ in range(80):
                pending = await asyncio.to_thread(
                    external.request, "GET", "/operations/" + pending["id"]
                )
                if pending["status"] == "canceled":
                    break
                await asyncio.sleep(0.05)
            assert pending["status"] == "canceled"
        finally:
            watch.cancel()
            event.cancel()
            await asyncio.gather(watch, event, return_exceptions=True)
            await client.close()

    try:
        asyncio.run(scenario())
    finally:
        stop.set()
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        for action in actions:
            action.destroy()
        adapter.destroy_node()
        robot.destroy_node()
        rclpy.try_shutdown()
