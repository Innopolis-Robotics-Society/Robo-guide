"""tool_broker.call_tool() поверх реального mission_fsm/narration_server (llm_plam.md §2/§8).

Критерий готовности шага 2: полный тур со сценарием прерывания и
stop_tour/pause/resume проходит ТОЛЬКО через call_tool(), без единого
прямого вызова ROS-клиента из теста и без ЛЛМ.
"""

from __future__ import annotations

from guide_robot_msgs.msg import CancelAll, MissionState
from test.mocks.harness import ToolBrokerTestHarness, pump_clock, wait_until

_S = MissionState


def _mission_state_is(harness: ToolBrokerTestHarness, target: int):
    def _predicate() -> bool:
        state = harness.broker.last_mission_state()
        return state is not None and state.state == target

    return _predicate


def test_guide_to_single_stop_completes_via_broker_only() -> None:
    harness = ToolBrokerTestHarness()
    try:
        harness.fixtures.add_exhibit("lab105a", ["Раз.", "Два."], version="rev1")
        harness.fixtures.add_location("lab105a", x=1.0, y=2.0)
        harness.nav.duration_s = 0.05
        harness.say.chars_per_sec = 50.0

        result = harness.broker.call_tool("guide_to", {"location_id": "lab105a"})
        assert result.ok, result.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_IDLE), step=0.2)
    finally:
        harness.shutdown()


def test_start_tour_gate_rejects_second_call_while_active() -> None:
    harness = ToolBrokerTestHarness()
    try:
        harness.fixtures.add_exhibit("lab105a", ["Раз.", "Два."], version="rev1")
        harness.fixtures.add_location("lab105a", x=1.0, y=2.0)
        harness.nav.duration_s = 5.0  # держим NAVIGATING достаточно долго

        first = harness.broker.call_tool("guide_to", {"location_id": "lab105a"})
        assert first.ok, first.message
        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)

        second = harness.broker.call_tool("guide_to", {"location_id": "lab105a"})
        assert not second.ok
        assert "недоступен" in second.message
    finally:
        harness.shutdown()


def test_stop_tour_gated_rejected_when_idle() -> None:
    harness = ToolBrokerTestHarness()
    try:
        result = harness.broker.call_tool("stop_tour", {})
        assert not result.ok
        assert "недоступен" in result.message
    finally:
        harness.shutdown()


def test_pause_resume_and_stop_tour_flow_via_broker() -> None:
    harness = ToolBrokerTestHarness()
    try:
        harness.fixtures.add_exhibit("lab105a", ["Раз.", "Два.", "Три."], version="rev1")
        harness.fixtures.add_location("lab105a", x=1.0, y=2.0)
        harness.nav.duration_s = 0.05
        harness.say.chars_per_sec = 5.0  # достаточно медленно, чтобы успеть паузу

        started = harness.broker.call_tool("guide_to", {"location_id": "lab105a"})
        assert started.ok, started.message
        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_NARRATING), step=0.1)

        # pause гейтится только в NARRATING (llm_plam.md §4/tools/schema.py).
        paused = harness.broker.call_tool("pause", {})
        assert paused.ok, paused.message
        wait_until(_mission_state_is(harness, _S.STATE_PAUSED), timeout_s=5.0)

        resumed = harness.broker.call_tool("resume", {})
        assert resumed.ok, resumed.message
        wait_until(_mission_state_is(harness, _S.STATE_NARRATING), timeout_s=5.0)

        harness.say.chars_per_sec = 100.0
        stopped = harness.broker.call_tool("stop_tour", {})
        assert stopped.ok, stopped.message
        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_IDLE), step=0.2)
    finally:
        harness.shutdown()


def test_resume_gated_rejects_when_not_paused() -> None:
    harness = ToolBrokerTestHarness()
    try:
        result = harness.broker.call_tool("resume", {})
        assert not result.ok
        assert "недоступен" in result.message
    finally:
        harness.shutdown()


def test_finish_answer_skip_stop_via_broker_advances_tour() -> None:
    """barge-in -> finish_answer(outcome=SKIP_STOP) -> вторая остановка через tool_broker."""
    harness = ToolBrokerTestHarness()
    try:
        for i, stop_id in enumerate(("stop0", "stop1")):
            chunks = [f"{stop_id} ч0.", f"{stop_id} ч1."]
            harness.fixtures.add_exhibit(stop_id, chunks, version="r1")
            harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
        harness.nav.duration_s = 0.05
        harness.say.chars_per_sec = 10.0

        route_result = harness.broker.call_tool(
            "tour_by_points", {"location_ids": ["stop0", "stop1"]}
        )
        assert route_result.ok, route_result.message
        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_NARRATING), step=0.1)

        client = harness.make_client_node()
        cancel_pub = client.create_publisher(CancelAll, "/speech/cancel_all", 1)
        cancel_pub.publish(
            CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
        )
        wait_until(_mission_state_is(harness, _S.STATE_ANSWERING), timeout_s=5.0)

        # SubmitAnswer.Request.OUTCOME_SKIP_STOP == 1.
        finish = harness.broker.call_tool("finish_answer", {"outcome": 1})
        assert finish.ok, finish.message

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
        harness.say.chars_per_sec = 50.0
        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_IDLE), step=0.2)
    finally:
        harness.shutdown()


def test_list_locations_hides_non_public_via_broker() -> None:
    harness = ToolBrokerTestHarness()
    try:
        harness.fixtures.add_location("lobby", x=0.0, y=0.0, is_public=True)
        harness.fixtures.add_location("server_room", x=1.0, y=1.0, is_public=False)

        result = harness.broker.call_tool("list_locations", {})
        assert result.ok, result.message
        ids = {loc["id"] for loc in result.data["locations"]}
        assert ids == {"lobby"}
    finally:
        harness.shutdown()


def test_tell_about_gated_outside_tour_only() -> None:
    """tell_about разрешён только в STATE_IDLE -- вне тура narration_server свободен."""
    harness = ToolBrokerTestHarness()
    try:
        harness.fixtures.add_exhibit("dinosaurs", ["Динозавры жили давно."], version="rev1")

        idle_call = harness.broker.call_tool("tell_about", {"exhibit_id": "dinosaurs"})
        assert idle_call.ok, idle_call.message

        # Занимаем mission_fsm туром -- state уходит из IDLE, tell_about
        # обязан быть отклонён гейтом (не дожидаясь REJECTED("busy") от
        # narration_server -- он тут вообще ни при чём, гейт смотрит только
        # на /mission/state).
        harness.fixtures.add_exhibit("lab105a", ["Раз."], version="rev1")
        harness.fixtures.add_location("lab105a", x=1.0, y=2.0)
        harness.nav.duration_s = 5.0
        tour = harness.broker.call_tool("guide_to", {"location_id": "lab105a"})
        assert tour.ok, tour.message
        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)

        during_tour = harness.broker.call_tool("tell_about", {"exhibit_id": "dinosaurs"})
        assert not during_tour.ok
        assert "недоступен" in during_tour.message
    finally:
        harness.shutdown()
