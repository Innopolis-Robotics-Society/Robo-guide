"""Общая обвязка для тестов mission_fsm поверх реального narration_server + моков.

В отличие от `test/mocks/*` (моки чужих узлов), здесь -- сборка РЕАЛЬНЫХ
узлов под тестом (`mission_fsm_node.py`, `narration_server_node.py`) и
типовых фикстур тура. Общий модуль, а не дублирование в каждом
test_*.py -- в отличие от узкого синхронизационного хелпера вроде
`_tick`/`_drain_to_completion` (которые каждый test_*.py держит своей
копией, документируя СВОЙ выбор шага прыжка часов), сборка нескольких
реальных нод с фикстурами -- это инфраструктура теста, а не логика теста;
три копии одного и того же here были бы чистым шумом.
"""

from __future__ import annotations

import time

from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import MissionState, SpeakingStatus
from rclpy.action import ActionClient
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter

from guide_robot_mission_control.lib.qos import QOS_MISSION_STATE, QOS_VOICE_SPEAKING
from guide_robot_mission_control.mission_fsm_node import MissionFsmNode
from guide_robot_mission_control.narration_server_node import NarrationServerNode
from test.mocks.harness import MissionTestHarness, wait_until

__all__ = [
    "make_fsm_node",
    "make_narration_node",
    "make_run_tour_client",
    "pump_clock",
    "setup_single_stop_tour",
    "speaking_started_count",
    "state_is",
    "state_listener",
]


def make_narration_node(harness: MissionTestHarness, **overrides: object) -> NarrationServerNode:
    values: dict[str, object] = {
        "resume_bridge_enabled": False,
        "hard_stop_result_timeout_s": 1.0,
    }
    values.update(overrides)
    params = [Parameter("use_sim_time", value=True)]
    params.extend(Parameter(name, value=value) for name, value in values.items())
    node = NarrationServerNode(context=harness.context, parameter_overrides=params)
    harness.add_node(node)
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    return node


def make_fsm_node(harness: MissionTestHarness, **overrides: object) -> MissionFsmNode:
    params = [Parameter("use_sim_time", value=True)]
    params.extend(Parameter(name, value=value) for name, value in overrides.items())
    node = MissionFsmNode(context=harness.context, parameter_overrides=params)
    harness.add_node(node)
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    return node


def make_run_tour_client(harness: MissionTestHarness):
    client_node = harness.make_client_node()
    client = ActionClient(client_node, RunTour, "run_tour")
    assert client.wait_for_server(timeout_sec=5.0)
    return client_node, client


def setup_single_stop_tour(
    harness: MissionTestHarness,
    *,
    location_id: str = "lab105a",
    chunks: list[str] | None = None,
    nav_duration_s: float = 0.05,
) -> None:
    """Одна остановка, один экспонат -- минимальный тур для тестов вне test_tour_flow.py."""
    chunks = chunks if chunks is not None else ["Раз.", "Два.", "Три."]
    harness.fixtures.add_exhibit(location_id, chunks, version="rev1")
    harness.fixtures.add_location(location_id, x=1.0, y=2.0)
    harness.nav.duration_s = nav_duration_s
    harness.nav.distance_m = 1.0


def state_listener(client_node) -> dict:
    state = {"latest": None, "count": 0, "history": []}

    def _on_state(msg: MissionState) -> None:
        state["latest"] = msg
        state["count"] += 1
        state["history"].append(msg)

    client_node.create_subscription(MissionState, "/mission/state", _on_state, QOS_MISSION_STATE)
    return state


def state_is(state: dict, target: int):
    """Предикат для `wait_until`/`pump_clock`: последний `/mission/state` уже == `target`."""
    return lambda: state["latest"] is not None and state["latest"].state == target


def speaking_started_count(client_node) -> list[int]:
    """Число РАЗНЫХ goal_id, замеченных с speaking=True -- см. test_narration_resume.py.

    Не счётчик фронтов False->True: /voice/speaking живёт на QoS depth=1,
    и при двух публикациях подряд быстрее, чем подписчик успевает их
    разобрать, промежуточное speaking=False может быть вытеснено из
    очереди раньше доставки.
    """
    seen_goal_ids: set[str] = set()
    count = [0]

    def _on_status(msg: SpeakingStatus) -> None:
        if msg.speaking and msg.goal_id not in seen_goal_ids:
            seen_goal_ids.add(msg.goal_id)
            count[0] = len(seen_goal_ids)

    client_node.create_subscription(
        SpeakingStatus, "/voice/speaking", _on_status, QOS_VOICE_SPEAKING
    )
    return count


def pump_clock(  # noqa: PLR0913 -- параметры одного синхронизационного хелпера, не команды
    harness: MissionTestHarness,
    predicate,
    *,
    step: float,
    max_iterations: int = 40,
    settle_s: float = 0.02,
    timeout_s: float = 15.0,
) -> None:
    """Прыгать часами шагом `step`, пока predicate() не станет True, либо не кончится бюджет."""
    for _ in range(max_iterations):
        if predicate():
            return
        harness.clock.advance(step)
        time.sleep(settle_s)
    wait_until(predicate, timeout_s=timeout_s)
