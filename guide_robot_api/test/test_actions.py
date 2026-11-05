"""Exercise real command adapter with ROS action client doubles and generated messages."""

import asyncio
from types import SimpleNamespace

import pytest
from guide_robot_api.command_adapter import CommandAdapter
from guide_robot_api.lib.operations import ActionRegistry

from guide_robot_msgs.action import RunTour, Say


class Adapter(CommandAdapter):
    def __init__(self, accepted=True, timeout=False):
        self._loop = asyncio.get_running_loop()
        self._operations = ActionRegistry()
        self._service_timeout_s = 1
        self.result = self._loop.create_future()
        self.accepted, self.timeout = accepted, timeout
        self.sent = []
        self.handle = SimpleNamespace(
            accepted=accepted,
            goal_id=SimpleNamespace(uuid=[1] * 16),
            get_result_async=lambda: self.result,
            cancel_goal_async=lambda: SimpleNamespace(goals_canceling=[1]),
        )
        client = SimpleNamespace(server_is_ready=lambda: True, send_goal_async=self.send)
        self._run_tour_client = self._say_client = client

    def send(self, goal, feedback_callback):
        self.sent.append(goal)
        self.feedback = feedback_callback
        return self.handle

    async def _call_ros(self, make, timeout):
        value = make()
        if self.timeout:
            raise TimeoutError()
        return value


@pytest.mark.parametrize("kind,Action", [("mission", RunTour), ("speech", Say)])
@pytest.mark.parametrize("status,expected", [(4, "succeeded"), (5, "canceled"), (6, "failed")])
def test_accept_feedback_result_cancel(kind, Action, status, expected):
    async def go():
        adapter = Adapter()
        payload = {"tour_id": "test"} if kind == "mission" else {"text": "hello"}
        code, op = await adapter._action(kind, {"_operation_id": "op", **payload})
        assert code == 202 and op["status"] == "running"
        adapter.feedback(SimpleNamespace(feedback=Action.Feedback()))
        await asyncio.sleep(0)
        assert op["feedback"] is not None
        code, data = await adapter.dispatch("POST", "operations/op/cancel", {})
        assert code == 202 and data["accepted"]
        adapter.result.set_result(SimpleNamespace(status=status, result=Action.Result()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert op["status"] == expected
        assert op["result"] is not None
        await adapter._action(kind, {"_operation_id": "op", **payload})
        assert len(adapter.sent) == 1

    asyncio.run(go())


@pytest.mark.parametrize(
    "accepted,timeout,code,status", [(False, False, 409, "rejected"), (True, True, 504, "unknown")]
)
def test_rejection_and_unknown(accepted, timeout, code, status):
    async def go():
        adapter = Adapter(accepted, timeout)
        actual, _ = await adapter._action("mission", {"_operation_id": "op", "tour_id": "test"})
        assert actual == code
        assert adapter._operations.items["op"]["status"] == status

    asyncio.run(go())
