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
from guide_robot_msgs.msg import CancelAll, MissionState
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

    # НЕ "state["latest"] is not None" -- mission_fsm.on_activate() публикует
    # IDLE ещё до этого goal-а (retained TRANSIENT_LOCAL сэмпл), тот
    # предикат стал бы истинным мгновенно и не гарантировал бы, что
    # _execute_run_tour уже успел выставить _active_ctx (гейт
    # _on_run_tour_goal). NAVIGATING -- первое состояние, которое
    # _execute_run_tour публикует ПОСЛЕ этого присваивания.
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)

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


def test_cancel_from_navigating_stops_in_place_not_returning_home(
    harness: MissionTestHarness,
) -> None:
    """stage2 A6: живой баг -- "стоп" уводил в RETURNING, робот уезжал на базу,
    бросая посетителя. Отмена по инициативе посетителя теперь терминальна на
    месте: RETURNING не появляется в истории состояний, домой не едет (ровно
    один NavigateToPose, не два), detail в IDLE отличим от обычного конца тура.
    """
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, state, _fsm = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=True,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 1

    cancel_future = goal_handle.cancel_goal_async()
    wait_for_future(cancel_future, timeout_s=15.0)
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)

    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_CANCELED
    assert harness.nav.goals_received == 1, "не должен был отправлять NavigateToPose домой"
    assert all(msg.state != MissionState.STATE_RETURNING for msg in state["history"])

    wait_until(state_is(state, MissionState.STATE_IDLE), timeout_s=15.0)
    assert "отмен" in state["latest"].detail
    del client_node


def test_hold_position_pauses_navigation_and_resumes_to_same_stop(
    harness: MissionTestHarness,
) -> None:
    """stage2 D3: hold_position -- отменяет активный NavigateToPose, стоит в PAUSED
    с pause_reason=PAUSE_USER, resume заново шлёт NavigateToPose на ТУ ЖЕ остановку
    (tour.index не двигался -- "сохранение цели" не требует отдельного поля)."""
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, state, fsm_node = _base_stack(harness)

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

    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 1

    fsm_node.request_pause()
    wait_until(state_is(state, MissionState.STATE_PAUSED), timeout_s=15.0)
    assert state["latest"].pause_reason == MissionState.PAUSE_USER
    assert harness.nav.goals_received == 1, "resume ещё не звался -- второй NavigateToPose рано"

    fsm_node.request_resume()
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 2, "resume обязан заново отправить NavigateToPose"
    assert state["latest"].stop_id == stop_ids[0], "цель после resume та же остановка"

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    del client_node


def test_transit_narration_fires_once_per_leg_without_repeats(
    harness: MissionTestHarness,
) -> None:
    """Блок E: во время долгого перегона и тишины звучит один транзитный чанк за
    ход навигации, чанки не повторяются за весь тур (2 чанка, 3 перегона)."""
    stop_ids = _setup_three_stop_tour(harness)
    harness.fixtures.add_tour(
        "lab_demo",
        "Тестовая экскурсия",
        [(sid, sid, 0, "short") for sid in stop_ids],
        transit_content_id="transit_lab",
    )
    harness.fixtures.add_exhibit("transit_lab", ["Транзит раз.", "Транзит два."], version="rev1")
    harness.nav.duration_s = 1.0  # дольше transit_after_s, чтобы транзит успел сработать

    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(
        harness, nav_stop_timeout_s=5.0, confirm_timeout_s=3.0, transit_after_s=0.2
    )
    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(tour_id="lab_demo", greet=False, narrate=True, confirm_between_stops=False)
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.05, max_iterations=400)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED

    transit_spoken = [t for t in harness.say.texts_received if t.startswith("Транзит")]
    assert transit_spoken == ["Транзит раз.", "Транзит два."], transit_spoken
    del client_node, fsm_node, state


def test_transit_still_speaking_does_not_skip_stop(harness: MissionTestHarness) -> None:
    """Прибытие посреди транзитного Narrate не пропускает остановку.

    Раньше fire-and-forget транзит держал `_active_execution`; Narrate
    остановки получал OUTCOME_REJECTED("busy") и `_skip_stop` вёл дальше
    без рассказа (воспроизведено вживую на lab_demo).
    """
    stop_ids = _setup_three_stop_tour(harness)
    harness.fixtures.add_tour(
        "lab_demo",
        "Тестовая экскурсия",
        [(sid, sid, 0, "short") for sid in stop_ids],
        transit_content_id="transit_lab",
    )
    long_transit = "Транзит " + ("слово " * 40)
    harness.fixtures.add_exhibit("transit_lab", [long_transit], version="rev1")
    harness.nav.duration_s = 0.4
    harness.say.chars_per_sec = 8.0

    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(
        harness, nav_stop_timeout_s=5.0, confirm_timeout_s=3.0, transit_after_s=0.05
    )
    client_node, run_tour_client = make_run_tour_client(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(tour_id="lab_demo", greet=False, narrate=True, confirm_between_stops=False)
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.05, max_iterations=400)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_skipped == 0
    assert result.stops_completed == 3
    spoken = harness.say.texts_received
    assert any(t.startswith("stop0") for t in spoken), spoken
    del client_node, fsm_node


def test_redirect_rejected_when_no_tour_active(harness: MissionTestHarness) -> None:
    """stage2 B2: в IDLE (нет активного тура) редирект отклоняется."""
    _client_node, _run_tour_client, _state, fsm_node = _base_stack(harness)
    accepted, message = fsm_node.redirect("stop0")
    assert not accepted
    assert message


def test_redirect_rejected_for_unknown_location(harness: MissionTestHarness) -> None:
    """stage2 B2: `~/redirect` валидирует `location_id` синхронно, до похода в FSM-поток."""
    stop_ids = _setup_three_stop_tour(harness)
    _client_node, run_tour_client, state, fsm_node = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=True,
        )
    )
    wait_for_future(goal_future)
    assert goal_future.result().accepted
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)

    accepted, message = fsm_node.redirect("no_such_location")
    assert not accepted
    assert "no_such_location" in message


def test_redirect_mid_tour_skips_home_and_ends_in_place(harness: MissionTestHarness) -> None:
    """stage2 B2, приёмка п.3: редирект во время тура едет к цели, рассказывает про
    неё, говорит прощальную фразу и заканчивает тур на месте, без RETURNING.
    """
    stop_ids = _setup_three_stop_tour(harness)
    harness.fixtures.add_exhibit("lidar_stand", ["Про лидар."], version="rev1")
    harness.fixtures.add_location("lidar_stand", x=9.0, y=9.0)
    client_node, run_tour_client, state, fsm_node = _base_stack(harness)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=stop_ids,
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=True,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)

    accepted, message = fsm_node.redirect("lidar_stand")
    assert accepted, message

    pump_clock(harness, state_is(state, MissionState.STATE_NARRATING), step=_NAV_DURATION_S + 0.02)
    assert state["latest"].stop_id == "lidar_stand"
    assert state["latest"].exhibit_id == "lidar_stand"

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)

    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.detail == "redirected"
    assert all(msg.state != MissionState.STATE_RETURNING for msg in state["history"])

    wait_until(state_is(state, MissionState.STATE_IDLE), timeout_s=15.0)
    assert "редирект" in state["latest"].detail
    del client_node



def _start_driving_tour(harness: MissionTestHarness, *, leg_s: float = 2.0):
    stop_ids = _setup_three_stop_tour(harness)
    client_node, run_tour_client, state, fsm_node = _base_stack(harness)
    harness.nav.duration_s = leg_s
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
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 1
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)
    return client_node, goal_handle, state, fsm_node, cancel_all_pub


def _cancel_tour(harness: MissionTestHarness, goal_handle) -> None:
    """Довести RunTour до конца -- живой поток тура между тестами роняет rclpy (segfault)."""
    wait_for_future(goal_handle.cancel_goal_async(), timeout_s=15.0)
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)


def _wakeword(pub, reason: str = CancelAll.REASON_WAKEWORD) -> None:
    pub.publish(CancelAll(scope=CancelAll.SCOPE_ALL, reason=reason))


def test_question_while_driving_keeps_driving_and_defers_narration_until_answered(
    harness: MissionTestHarness,
) -> None:
    """Вопрос по дороге не останавливает робота; прибытие за время ответа не
    запускает рассказ точки -- он начнётся только после конца ответа, без
    повторного NavigateToPose."""
    _client, goal_handle, state, fsm_node, cancel_all_pub = _start_driving_tour(harness)

    _wakeword(cancel_all_pub)
    wait_until(state_is(state, MissionState.STATE_ANSWERING), timeout_s=15.0)
    assert state["latest"].base_state == MissionState.STATE_NAVIGATING

    # Виртуальное время дольше перегона (2 с) -- робот доехал, ответ ещё идёт.
    for _ in range(40):
        harness.clock.advance(0.1)
        time.sleep(0.01)
    assert state["latest"].state == MissionState.STATE_ANSWERING
    assert harness.nav.goals_received == 1

    fsm_node.submit_answer("это лаборатория")
    wait_until(state_is(state, MissionState.STATE_NARRATING), timeout_s=15.0)
    assert harness.nav.goals_received == 1, "доехавший goal не должен пересылаться"
    assert state["latest"].stop_index == 0

    _cancel_tour(harness, goal_handle)


def test_question_while_driving_answer_before_arrival_continues_same_goal(
    harness: MissionTestHarness,
) -> None:
    """Ответ закончился до прибытия -- NAVIGATING подхватывает тот же goal, не шлёт второй."""
    _client, goal_handle, state, fsm_node, cancel_all_pub = _start_driving_tour(
        harness, leg_s=100.0
    )

    _wakeword(cancel_all_pub)
    wait_until(state_is(state, MissionState.STATE_ANSWERING), timeout_s=15.0)
    fsm_node.submit_answer("это по дороге")
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    time.sleep(0.2)
    assert harness.nav.goals_received == 1

    _cancel_tour(harness, goal_handle)


def test_stop_phrase_while_driving_pauses_navigation(harness: MissionTestHarness) -> None:
    """Стоп-слово на ходу -- замолчать и встать (PAUSED); resume -- та же остановка."""
    _client, goal_handle, state, fsm_node, cancel_all_pub = _start_driving_tour(
        harness, leg_s=100.0
    )

    _wakeword(cancel_all_pub, CancelAll.REASON_STOP_PHRASE)
    wait_until(state_is(state, MissionState.STATE_PAUSED), timeout_s=15.0)
    assert state["latest"].pause_reason == MissionState.PAUSE_USER

    fsm_node.request_resume()
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 2
    assert state["latest"].stop_index == 0

    _cancel_tour(harness, goal_handle)


@pytest.mark.parametrize("how", ["stop_phrase", "hold_position"])
def test_stop_while_answering_on_the_way_pauses_and_resumes_same_stop(
    harness: MissionTestHarness, how: str
) -> None:
    """Стоп-слово/«подожди» посреди ответа на ходу -- PAUSED (не снова ехать после ответа)."""
    _client, goal_handle, state, fsm_node, cancel_all_pub = _start_driving_tour(
        harness, leg_s=100.0
    )

    _wakeword(cancel_all_pub)
    wait_until(state_is(state, MissionState.STATE_ANSWERING), timeout_s=15.0)
    if how == "stop_phrase":
        _wakeword(cancel_all_pub, CancelAll.REASON_STOP_PHRASE)
    else:
        fsm_node.request_pause()
    wait_until(state_is(state, MissionState.STATE_PAUSED), timeout_s=15.0)
    assert state["latest"].interrupt == MissionState.IRQ_NONE

    fsm_node.request_resume()
    wait_until(state_is(state, MissionState.STATE_NAVIGATING), timeout_s=15.0)
    assert harness.nav.goals_received == 2, "после паузы goal к той же остановке шлётся заново"
    assert state["latest"].stop_index == 0

    _cancel_tour(harness, goal_handle)


def test_hold_position_during_answering_after_narration_is_dropped(
    harness: MissionTestHarness,
) -> None:
    """Стоящий робот: «подожди» в ANSWERING не должен остановить СЛЕДУЮЩИЙ перегон."""
    _client, goal_handle, state, fsm_node, cancel_all_pub = _start_driving_tour(harness)
    harness.nav.duration_s = 0.5
    pump_clock(harness, state_is(state, MissionState.STATE_NARRATING), step=0.1)

    _wakeword(cancel_all_pub)
    wait_until(state_is(state, MissionState.STATE_ANSWERING), timeout_s=15.0)
    fsm_node.request_pause()
    time.sleep(0.2)
    assert state["latest"].state == MissionState.STATE_ANSWERING
    fsm_node.submit_answer("ок")

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=300)
    wait_for_future(result_future, timeout_s=15.0)
    assert result_future.result().result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert all(m.state != MissionState.STATE_PAUSED for m in state["history"])
