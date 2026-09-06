"""dialog_agent end-to-end на реальном mission-стеке + MockLlmServer (llm_plam.md §5/§6/§9).

`ToolBrokerTestHarness` (шаг 8 плана) поднимает `DialogAgentNode` рядом с
`tool_broker`/`mission_fsm`/`narration_server`, направленным на
`MockLlmServer` (шаг 4) вместо настоящего `llm_server/`. Проверяется путь
целиком: транскрипт -> ReAct-ход -> `~/call_tool` -> реальный `tool_broker`
-> реальный mission-стек -> мок голоса/навигации.
"""

from __future__ import annotations

import time

from guide_robot_msgs.msg import CancelAll, MissionState, Transcript
from test.mocks.harness import ToolBrokerTestHarness, pump_clock, wait_until
from test.mocks.mock_llm_server import MockLlmServer

_S = MissionState


def _mission_state_is(harness: ToolBrokerTestHarness, target: int):
    def _predicate() -> bool:
        state = harness.broker.last_mission_state()
        return state is not None and state.state == target

    return _predicate


def _dialog_agent_has_mission_state(harness: ToolBrokerTestHarness):
    return lambda: harness.dialog_agent.last_mission_state() is not None


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def _setup_two_stop_tour(harness: ToolBrokerTestHarness) -> None:
    for i, stop_id in enumerate(("stop0", "stop1")):
        harness.fixtures.add_exhibit(stop_id, [f"{stop_id} ч0.", f"{stop_id} ч1."], version="r1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = 0.05
    harness.say.chars_per_sec = 50.0


def test_transcript_in_idle_drives_say_through_call_tool() -> None:
    """Полный путь: транскрипт -> ЛЛМ (мок) -> tool_broker.call_tool("say") -> MockSayServer."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.chunks = ['{"tool": "say", "args": {"text": "Привет!"}}']

        client = harness.make_client_node()
        _publish_transcript(client, "привет")

        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
    finally:
        harness.shutdown()


def test_barge_in_aborts_in_flight_turn_before_tool_executes() -> None:
    """llm_plam.md §6: abort реального HTTP-запроса -- say() для оборванного хода не зовётся."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        harness.llm_server.chunks = ["раз", "два", "три", "четыре", "пять"]
        harness.llm_server.chunk_delay_s = 0.3

        client = harness.make_client_node()
        _publish_transcript(client, "расскажи что-нибудь длинное")
        time.sleep(0.15)  # дать ходу начаться и получить хотя бы первый чанк

        cancel_pub = client.create_publisher(CancelAll, "/speech/cancel_all", 1)
        cancel_pub.publish(CancelAll(reason=CancelAll.REASON_BARGE_IN))

        time.sleep(1.0)  # пережить остаток MODE_SLOW-стрима, если abort не сработал
        assert harness.say.goals_received == 0, "say() не должен был вызваться -- ход оборван"

        # Агент обязан разблокироваться после abort -- следующий ход проходит штатно.
        harness.llm_server.mode = MockLlmServer.MODE_OK
        harness.llm_server.chunks = ['{"tool": "say", "args": {"text": "ок"}}']
        _publish_transcript(client, "ещё раз")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
    finally:
        harness.shutdown()


def test_fast_path_confirm_suppresses_llm_call() -> None:
    """llm_plam.md §9: уверенный локальный матч -- ЛЛМ вообще не запрашивается."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool("tour_by_points", {"location_ids": ["stop0", "stop1"]})
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)
        wait_until(
            lambda: harness.dialog_agent.last_mission_state().state == _S.STATE_AWAITING_CONFIRM,
            timeout_s=5.0,
        )

        client = harness.make_client_node()
        _publish_transcript(client, "да, давайте")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
        assert harness.llm_server.last_request_body is None, "ЛЛМ не должен был вызываться вовсе"
    finally:
        harness.shutdown()
