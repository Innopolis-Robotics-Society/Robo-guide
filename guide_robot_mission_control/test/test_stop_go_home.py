"""Task A: `~/request_stop` (A1), `~/go_home` (A2), chunk-прогресс (A3).

См. `task_A_mission_control_prereq.md` -- критерии приёмки этого файла
зеркалят пункты 1-9 оттуда (кроме тех, что требуют живого стенда: точная
целевая поза `~/go_home` в `/navigate_to_pose/_action/status` и латентность
чанк-фидбека по jsonl-таймстемпам).
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.action import Narrate, RunTour
from guide_robot_msgs.msg import CancelAll, MissionState
from rclpy.action import ActionClient
from std_msgs.msg import Bool
from std_srvs.srv import Trigger

from test.mission_fsm_test_helpers import (
    make_fsm_node,
    make_narration_node,
    make_run_tour_client,
    pump_clock,
    setup_single_stop_tour,
    state_is,
    state_listener,
)
from test.mocks.harness import MissionTestHarness, wait_for_future, wait_until

_NAV_DURATION_S = 0.05


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _publish_estop_until_seen(client_node, fsm_node, *, value: bool) -> None:
    """Публиковать /supervisor/estop, пока узел реально его не увидел.

    Один publish() может уйти до того, как DDS-подписка mission_fsm
    обнаружит нового паблишера (см. комментарий-прецедент в
    test_safety_hold.py) -- здесь то же самое, но overt-повтором вместо
    предварительной паузы на "рукопожатие".
    """
    pub = client_node.create_publisher(Bool, "/supervisor/estop", 10)
    deadline = time.monotonic() + 5.0
    while fsm_node._estop != value and time.monotonic() < deadline:
        pub.publish(Bool(data=value))
        time.sleep(0.05)


# -- A1: ~/request_stop ----------------------------------------------------


def test_request_stop_without_active_anything_reports_no_active_tour(
    harness: MissionTestHarness,
) -> None:
    fsm_node = make_fsm_node(harness)
    response = fsm_node._srv_request_stop(Trigger.Request(), Trigger.Response())
    assert response.success is False
    assert response.message == "no_active_tour"


@pytest.mark.parametrize(
    "target_state",
    ["navigating", "narrating", "paused", "awaiting_confirm", "answering"],
)
def test_request_stop_cancels_from_each_state(
    harness: MissionTestHarness, target_state: str
) -> None:
    """A1, критерий 3: работает из NAVIGATING/NARRATING/PAUSED/AWAITING_CONFIRM/ANSWERING.

    Во всех случаях -- OUTCOME_CANCELED, робот не едет домой (return_home=True
    в goal-е нарочно, чтобы отличить "стоп на месте" от случайного успеха
    из-за return_home=False), RETURNING не появляется в истории состояний.
    """
    two_stops = target_state == "awaiting_confirm"
    if two_stops:
        for i, stop_id in enumerate(("lab105a", "lab106")):
            harness.fixtures.add_exhibit(stop_id, ["Раз.", "Два."], version="rev1")
            harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
        stop_ids = ["lab105a", "lab106"]
    else:
        setup_single_stop_tour(harness)
        stop_ids = ["lab105a"]
    harness.nav.duration_s = _NAV_DURATION_S
    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(harness, nav_stop_timeout_s=5.0, confirm_timeout_s=100.0)
    harness.say.chars_per_sec = 10.0

    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=two_stops,
            return_home=True,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    if target_state == "navigating":
        wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    elif target_state == "narrating":
        pump_clock(
            harness, state_is(state, MissionState.STATE_NARRATING), step=_NAV_DURATION_S + 0.02
        )
        time.sleep(0.05)
    elif target_state == "paused":
        wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
        fsm_node.request_pause()
        wait_until(state_is(state, MissionState.STATE_PAUSED), timeout_s=15.0)
    elif target_state == "awaiting_confirm":
        harness.say.chars_per_sec = 50.0
        pump_clock(
            harness,
            state_is(state, MissionState.STATE_AWAITING_CONFIRM),
            step=_NAV_DURATION_S + 0.02,
            max_iterations=200,
        )
    else:  # answering -- barge-in поверх NARRATING (design §5.2)
        pump_clock(
            harness, state_is(state, MissionState.STATE_NARRATING), step=_NAV_DURATION_S + 0.02
        )
        time.sleep(0.05)
        cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)
        cancel_all_pub.publish(
            CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
        )
        wait_until(state_is(state, MissionState.STATE_ANSWERING), timeout_s=15.0)

    nav_goals_before_stop = harness.nav.goals_received
    fsm_node.request_stop()

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_CANCELED

    wait_until(state_is(state, MissionState.STATE_IDLE), timeout_s=15.0)
    assert all(msg.state != MissionState.STATE_RETURNING for msg in state["history"])
    assert harness.nav.goals_received == nav_goals_before_stop, "не едет домой"
    del client_node


def test_request_stop_during_transit_narration_frees_narration_server(
    harness: MissionTestHarness,
) -> None:
    """A1, критерий 3a: стоп из NAVIGATING во время транзитного Narrate.

    Тот же сценарий, что и regression-тест
    test_tour_flow.py::test_transit_still_speaking_does_not_skip_stop, но
    здесь останавливаем явно ~/request_stop вместо того, чтобы дать
    прибытию случиться -- narration_server обязан освободиться в любом
    случае, не только на штатном выходе из состояния.
    """
    stop_ids = ["stop0", "stop1"]
    for i, stop_id in enumerate(stop_ids):
        harness.fixtures.add_exhibit(stop_id, [f"{stop_id} чанк."], version="rev1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.fixtures.add_tour(
        "lab_demo",
        "Тестовая экскурсия",
        [(sid, sid, 0, "short") for sid in stop_ids],
        transit_content_id="transit_lab",
    )
    long_transit = "Транзит " + ("слово " * 40)
    harness.fixtures.add_exhibit("transit_lab", [long_transit], version="rev1")
    harness.nav.duration_s = 2.0  # долгий перегон -- транзит успевает реально зазвучать
    harness.say.chars_per_sec = 8.0

    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(harness, nav_stop_timeout_s=5.0, transit_after_s=0.05)
    client_node, run_tour_client = make_run_tour_client(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(tour_id="lab_demo", greet=False, narrate=True, confirm_between_stops=False)
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    # Дождаться, что транзитный Say реально ушёл -- иначе стоп может прийти
    # раньше, чем NavigatingState вообще отправил Narrate. Пейсинг мока идёт
    # по sim-часам -- их надо крутить самим (transit_after_s=0.05 не наступит
    # без advance()), тем же step, что и nav.duration_s -- не проскочить.
    pump_clock(
        harness,
        lambda: harness.say.goals_received >= 1,
        step=0.1,
        max_iterations=200,
    )
    time.sleep(0.05)

    fsm_node.request_stop()
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.05, max_iterations=400)
    wait_for_future(result_future, timeout_s=15.0)
    assert result_future.result().result.outcome == RunTour.Result.OUTCOME_CANCELED

    # narration_server обязан освободиться -- последующий Narrate проходит.
    narrate_client = ActionClient(client_node, Narrate, "narrate")
    assert narrate_client.wait_for_server(timeout_sec=5.0)
    harness.say.chars_per_sec = 50.0
    narrate_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="stop0"))
    wait_for_future(narrate_future, timeout_s=15.0)
    narrate_handle = narrate_future.result()
    assert narrate_handle.accepted, "narration_server остался занят брошенным транзитным goal-ом"


# -- A2: ~/go_home -----------------------------------------------------------


def test_go_home_rejected_while_tour_active(harness: MissionTestHarness) -> None:
    setup_single_stop_tour(harness)
    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(harness, nav_stop_timeout_s=5.0)
    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=["lab105a"],
            greet=False,
            narrate=False,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    assert goal_future.result().accepted
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)

    accepted, message = fsm_node.go_home()
    assert accepted is False
    assert message == "tour_active"
    del client_node


def test_go_home_rejected_when_safety_hold_active(harness: MissionTestHarness) -> None:
    fsm_node = make_fsm_node(harness)
    client_node = harness.make_client_node()
    _publish_estop_until_seen(client_node, fsm_node, value=True)
    assert fsm_node._safety_hold_event.is_set()

    accepted, message = fsm_node.go_home()
    assert accepted is False
    assert message == "safety_hold"
    del client_node


def test_go_home_sends_nav_goal_and_completes(harness: MissionTestHarness) -> None:
    harness.nav.duration_s = _NAV_DURATION_S
    fsm_node = make_fsm_node(harness, home_pose=[3.0, 4.0, 0.0])
    client_node = harness.make_client_node()
    state = state_listener(client_node)

    accepted, message = fsm_node.go_home()
    assert accepted is True
    assert message == ""

    wait_until(state_is(state, MissionState.STATE_RETURNING), timeout_s=15.0)
    assert harness.nav.goals_received == 1

    pump_clock(
        harness,
        state_is(state, MissionState.STATE_IDLE),
        step=_NAV_DURATION_S + 0.02,
        max_iterations=200,
    )
    del client_node


def test_go_home_idempotent_while_already_returning(harness: MissionTestHarness) -> None:
    harness.nav.mode = harness.nav.MODE_HANG  # держим в пути -- второй вызов застаёт RETURNING
    fsm_node = make_fsm_node(harness)
    client_node = harness.make_client_node()
    state = state_listener(client_node)

    accepted, _message = fsm_node.go_home()
    assert accepted is True
    wait_until(state_is(state, MissionState.STATE_RETURNING), timeout_s=15.0)
    assert harness.nav.goals_received == 1

    accepted2, message2 = fsm_node.go_home()
    assert accepted2 is True
    assert message2 == "already_returning"
    assert harness.nav.goals_received == 1, "второй NavigateToPose не должен уйти"

    # Не оставлять зависший _run_standalone_return-поток живым после теста --
    # harness.shutdown() не проходит через lifecycle on_deactivate (значит,
    # deactivating_event никогда не взводится), а MODE_HANG иначе никогда
    # не отпустит фоновый поток: он бы пережил уничтожение rclpy.Context
    # этого теста и полез бы в мёртвые хендлы при спине следующего теста
    # (сегфолт, воспроизведено вживую при разработке этого файла).
    fsm_node.request_stop()
    wait_until(state_is(state, MissionState.STATE_IDLE), timeout_s=15.0)
    del client_node


def test_request_stop_during_standalone_return_cancels_and_stays_in_place(
    harness: MissionTestHarness,
) -> None:
    harness.nav.mode = harness.nav.MODE_HANG
    fsm_node = make_fsm_node(harness)
    client_node = harness.make_client_node()
    state = state_listener(client_node)

    accepted, _message = fsm_node.go_home()
    assert accepted is True
    wait_until(state_is(state, MissionState.STATE_RETURNING), timeout_s=15.0)

    fsm_node.request_stop()
    wait_until(state_is(state, MissionState.STATE_IDLE), timeout_s=15.0)
    assert harness.nav.goals_received == 1, "не должен переотправлять NavigateToPose"
    del client_node


def test_estop_during_standalone_return_holds_then_resumes(harness: MissionTestHarness) -> None:
    """A2, критерий 7: safety_hold во время активного возврата уходит в HELD, потом резюмирует."""
    harness.nav.duration_s = _NAV_DURATION_S
    harness.nav.mode = harness.nav.MODE_HANG
    fsm_node = make_fsm_node(harness, held_max_s=100.0)
    client_node = harness.make_client_node()
    state = state_listener(client_node)

    accepted, _message = fsm_node.go_home()
    assert accepted is True
    wait_until(state_is(state, MissionState.STATE_RETURNING), timeout_s=15.0)

    _publish_estop_until_seen(client_node, fsm_node, value=True)
    wait_until(state_is(state, MissionState.STATE_HELD), timeout_s=15.0)

    harness.nav.mode = harness.nav.MODE_SUCCEED
    _publish_estop_until_seen(client_node, fsm_node, value=False)
    wait_until(state_is(state, MissionState.STATE_RETURNING), timeout_s=15.0)

    pump_clock(
        harness,
        state_is(state, MissionState.STATE_IDLE),
        step=_NAV_DURATION_S + 0.02,
        max_iterations=200,
    )
    del client_node


# -- A3: chunk_index/chunk_total ----------------------------------------------


def test_narrate_feedback_populates_chunk_progress_and_resets_on_exit(
    harness: MissionTestHarness,
) -> None:
    """A3, критерии 8-9: chunk_total совпадает с числом чанков, chunk_index растёт
    монотонно от 0, оба обнуляются после выхода из NARRATING."""
    chunks = ["Раз.", "Два.", "Три."]
    setup_single_stop_tour(harness, chunks=chunks)
    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(harness, nav_stop_timeout_s=5.0)
    harness.say.chars_per_sec = 50.0

    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=["lab105a"],
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.05, max_iterations=400)
    wait_for_future(result_future, timeout_s=15.0)
    assert result_future.result().result.outcome == RunTour.Result.OUTCOME_COMPLETED

    # Читаем из истории /mission/state (реально доставленные сообщения), а не
    # живым поллингом "latest" -- пейсинг Say и публикация чанков идут по
    # sim-часам, которые здесь дальше не крутятся: живой поллинг гоняется бы
    # за pump_clock-скачками и мог поймать состояние уже ПОСЛЕ NARRATING
    # (chunk_index там уже 0 -- см. A3.5), спутав это со сбросом счётчика.
    narrating_msgs = [msg for msg in state["history"] if msg.state == MissionState.STATE_NARRATING]
    assert narrating_msgs, "NARRATING обязана была опубликоваться хотя бы раз"
    # Самая первая публикация NARRATING (on_enter -> on_state_changed) идёт ДО
    # первого Narrate.Feedback -- chunk_total там ещё 0, это ожидаемо, не баг
    # (см. фидбек-цепочку A3). Индексы смотрим только начиная с первого
    # реального фидбека.
    assert narrating_msgs[-1].chunk_total == len(chunks)
    indices = [msg.chunk_index for msg in narrating_msgs if msg.chunk_total == len(chunks)]
    deduped = [v for i, v in enumerate(indices) if i == 0 or v != indices[i - 1]]
    expected = list(range(len(chunks)))
    assert deduped == expected, f"chunk_index обязан расти монотонно от 0: {deduped} != {expected}"

    assert state["latest"].chunk_index == 0
    assert state["latest"].chunk_total == 0
    del fsm_node, client_node
