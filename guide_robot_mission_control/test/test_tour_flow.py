"""Полный цикл тура через RunTour (design §5.5, §9.2).

Синхронизация -- та же "накачка часов небольшими прыжками, пока предикат
не станет True" идиома, что и в test_narration_resume.py/test_interrupt_stack.py
(`pump_clock` из test/mission_fsm_test_helpers.py): navigate_to_pose и Say
пейсятся по sim-времени независимо друг от друга, поэтому шаг должен быть
достаточно мал, чтобы не проскочить состояние, которое тест ловит.
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import MissionState
from rclpy.action import ActionClient

from test.mission_fsm_test_helpers import (
    make_fsm_node,
    make_narration_node,
    make_run_tour_client,
    pump_clock,
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


def _setup_three_stop_tour(harness: MissionTestHarness) -> list[str]:
    stop_ids = ["stop0", "stop1", "stop2"]
    for i, stop_id in enumerate(stop_ids):
        chunks = [f"{stop_id} чанк0.", f"{stop_id} чанк1."]
        harness.fixtures.add_exhibit(stop_id, chunks, version="rev1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = _NAV_DURATION_S
    harness.say.chars_per_sec = 50.0
    return stop_ids


def _base_stack(harness: MissionTestHarness, **narration_overrides: object):
    make_narration_node(harness, lookahead=0, **narration_overrides)
    fsm_node = make_fsm_node(harness, nav_stop_timeout_s=3.0, confirm_timeout_s=3.0)
    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)
    return client_node, run_tour_client, state, fsm_node


def test_full_tour_three_stops_completes(harness: MissionTestHarness) -> None:
    stop_ids = _setup_three_stop_tour(harness)
    _client_node, run_tour_client, _state, _fsm = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
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
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_completed == 3
    assert result.stops_skipped == 0


def test_restart_with_start_index(harness: MissionTestHarness) -> None:
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, state, _fsm = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            start_index=1,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    # Первая NAVIGATING обязана указывать сразу на stop1, не stop0 -- рестарт с середины.
    assert state["latest"].stop_id == "stop1"
    assert state["latest"].stop_index == 1

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_completed == 2  # только stop1 и stop2
    del client_node


def test_nav_failed_skips_stop(harness: MissionTestHarness) -> None:
    stop_ids = _setup_three_stop_tour(harness)
    _client_node, run_tour_client, _state, _fsm = _base_stack(harness)
    harness.nav.mode_queue = [harness.nav.MODE_ABORT]  # только первый перегон не удаётся

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
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
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_skipped == 1
    assert result.stops_completed == 2


def test_narrate_rejected_skips_stop(harness: MissionTestHarness) -> None:
    """Контент не найден (design: Narrate REJECTED) -- остановка пропущена, тур не падает.

    Раньше `NARRATE_FAILED`/`ABORTED` не было в `_TRANSITIONS["narrating"]`
    -- RootStateMachine.run_tour() ронял необработанный RuntimeError,
    воспроизведено вживую (content_server не нашёл контент для реального
    exhibit_id). Зеркалит test_nav_failed_skips_stop, только источник
    сбоя -- отсутствующий контент, а не nav.mode_queue.
    """
    stop_ids = ["stop0", "stop1", "stop2"]
    for i, stop_id in enumerate(stop_ids):
        if stop_id != "stop1":  # у stop1 сознательно нет фикстуры контента
            harness.fixtures.add_exhibit(stop_id, [f"{stop_id} чанк0."], version="rev1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = _NAV_DURATION_S
    harness.say.chars_per_sec = 50.0
    _client_node, run_tour_client, _state, _fsm = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
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
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_skipped == 1
    assert result.stops_completed == 2


def test_pause_during_narrating_resumes_and_completes_stop(harness: MissionTestHarness) -> None:
    """PAUSED посреди NARRATING -> resume -> остановка ЗАВЕРШЕНА, не пропущена.

    Раньше `NarratingState.poll()` отдавал PAUSED, не остановив активный
    `Narrate` -- narration_server оставался занят брошенным goal-ом
    (`self._active_execution`); после resume новый `Narrate` получал
    OUTCOME_REJECTED("busy"), что через `_skip_stop` тихо пропускало
    остановку и заканчивало тур раньше времени (воспроизведено вживую:
    pause -> resume -> тур молча уехал домой). cancel_active_work() теперь
    вызывается явно из poll() при PAUSED, как при CANCELED/HELD.
    """
    stop_ids = ["stop0", "stop1"]
    for i, stop_id in enumerate(stop_ids):
        harness.fixtures.add_exhibit(stop_id, [f"{stop_id} ч0.", f"{stop_id} ч1."], version="rev1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = _NAV_DURATION_S
    harness.say.chars_per_sec = 10.0  # медленно -- есть время поймать NARRATING до конца чанка
    _client_node, run_tour_client, state, fsm_node = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    pump_clock(harness, state_is(state, MissionState.STATE_NARRATING), step=_NAV_DURATION_S + 0.02)
    time.sleep(0.05)  # дать narration_server реально принять Narrate-goal

    fsm_node.request_pause()
    wait_until(state_is(state, MissionState.STATE_PAUSED), timeout_s=15.0)
    assert state["latest"].resume_token != ""  # чанк 0 реально остановлен, не брошен

    fsm_node.request_resume()
    wait_until(state_is(state, MissionState.STATE_NARRATING), timeout_s=15.0)

    harness.say.chars_per_sec = 50.0
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_skipped == 0
    assert result.stops_completed == 2


def test_narrate_false_skips_narration(harness: MissionTestHarness) -> None:
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, _state, _fsm = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=False,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_completed == 3
    # narrate=False -- ни один Narrate/Say goal не должен был уйти наружу.
    # /mission/state ВСЁ РАВНО проходит через узел "narrating" в графе SM
    # (on_state_changed вызывается безусловно при входе в состояние,
    # раньше проверки tour.narrate) -- утверждение "STATE_NARRATING никогда
    # не публикуется" неверно по конструкции и держалось только на везении
    # с гонкой QoS-коалессирования; заменено на прямую проверку мока.
    assert harness.say.goals_received == 0
    del client_node


def test_second_run_tour_rejected_while_busy(harness: MissionTestHarness) -> None:
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, state, _fsm = _base_stack(harness)

    first_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(first_future)
    first_handle = first_future.result()
    assert first_handle.accepted

    wait_until(lambda: state["latest"] is not None, timeout_s=15.0)

    second_client_node = harness.make_client_node()
    second_client = ActionClient(second_client_node, RunTour, "run_tour")
    assert second_client.wait_for_server(timeout_sec=5.0)
    second_future = second_client.send_goal_async(RunTour.Goal(location_ids=stop_ids))
    wait_for_future(second_future)
    second_handle = second_future.result()
    assert not second_handle.accepted

    cancel_future = first_handle.cancel_goal_async()
    wait_for_future(cancel_future, timeout_s=15.0)
    result_future = first_handle.get_result_async()
    wait_for_future(result_future, timeout_s=15.0)
    del client_node


@pytest.mark.parametrize("target_state", ["greeting", "navigating", "narrating", "returning"])
def test_cancel_run_tour_in_each_state(harness: MissionTestHarness, target_state: str) -> None:
    # одна остановка -- быстрее добраться до RETURNING
    stop_ids = _setup_three_stop_tour(harness)[:1]
    client_node, run_tour_client, state, _fsm = _base_stack(harness)

    greet = target_state == "greeting"
    # нужен, чтобы RETURNING реально слал NavigateToPose и не завершался мгновенно
    return_home = True

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=greet,
            narrate=True,
            confirm_between_stops=False,
            return_home=return_home,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    if target_state == "greeting":
        wait_until(state_is(state, MissionState.STATE_GREETING), timeout_s=15.0)
    elif target_state == "navigating":
        wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    elif target_state == "narrating":
        pump_clock(
            harness, state_is(state, MissionState.STATE_NARRATING), step=_NAV_DURATION_S + 0.02
        )
        time.sleep(0.05)
    else:  # returning
        pump_clock(
            harness,
            state_is(state, MissionState.STATE_RETURNING),
            step=0.1,
            max_iterations=200,
        )

    cancel_future = goal_handle.cancel_goal_async()
    wait_for_future(cancel_future, timeout_s=15.0)

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_CANCELED
    del client_node
