"""POST /api/confirm на стороне ноды: гейт AWAITING_CONFIRM и маршрут ответа в сервисы.

Нода собирается без __init__ (без rclpy-графа): подставлены только поля и
вызовы сервисов, которые читает `_on_api_confirm`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from guide_robot_operator_ui.operator_ui_node import OperatorUiNode

from guide_robot_msgs.msg import MissionState


def _node(state: int, *, age_s: float = 0.0, stale_s: float = 3.0) -> Any:
    node = OperatorUiNode.__new__(OperatorUiNode)
    node._last_mission_msg = MissionState(state=state)
    node._mission_received_at_s = 100.0 - age_s
    node._mission_state_stale_s = stale_s
    node._service_timeout_s = 1.0
    node._now_s = lambda: 100.0
    node.calls = []
    node.trigger_status = {"request_question": 200, "prompt_listen": 200}

    async def call_trigger(client: object, *, name: str) -> tuple[int, dict]:
        del client
        node.calls.append(name)
        return node.trigger_status[name], {"message": ""}

    async def call_ros(make: Any, timeout_s: float, *, name: str) -> Any:
        del timeout_s
        node.calls.append(name)
        node.confirm_data = make().data
        return SimpleNamespace(success=True, message="")

    node._call_trigger = call_trigger
    node._call_ros = call_ros
    node._request_question_client = object()
    node._prompt_listen_client = object()
    node._submit_confirm_client = SimpleNamespace(call_async=lambda req: req)
    return node


def _confirm(node: Any, answer: str) -> tuple[int, dict]:
    return asyncio.run(node._on_api_confirm(answer=answer))


@pytest.mark.parametrize("answer", ["yes", "stop", "question"])
def test_refused_outside_awaiting_confirm(answer: str) -> None:
    node = _node(MissionState.STATE_NARRATING)
    assert _confirm(node, answer) == (409, {"message": "not_awaiting_confirm"})
    assert node.calls == []


def test_refused_on_stale_mission_state() -> None:
    node = _node(MissionState.STATE_AWAITING_CONFIRM, age_s=5.0)
    assert _confirm(node, "yes")[0] == 409
    assert node.calls == []


@pytest.mark.parametrize(("answer", "data"), [("yes", True), ("stop", False)])
def test_yes_and_stop_go_to_submit_confirm(answer: str, data: bool) -> None:
    node = _node(MissionState.STATE_AWAITING_CONFIRM)
    assert _confirm(node, answer)[0] == 200
    assert node.calls == ["submit_confirm"]
    assert node.confirm_data is data


def test_question_interrupts_then_opens_listening() -> None:
    node = _node(MissionState.STATE_AWAITING_CONFIRM)
    assert _confirm(node, "question") == (200, {"message": "", "listening": True})
    assert node.calls == ["request_question", "prompt_listen"]


def test_question_without_voice_window_still_succeeds() -> None:
    node = _node(MissionState.STATE_AWAITING_CONFIRM)
    node.trigger_status["prompt_listen"] = 503
    assert _confirm(node, "question") == (200, {"message": "", "listening": False})


def test_question_refused_by_fsm_does_not_open_listening() -> None:
    node = _node(MissionState.STATE_AWAITING_CONFIRM)
    node.trigger_status["request_question"] = 409
    assert _confirm(node, "question")[0] == 409
    assert node.calls == ["request_question"]
