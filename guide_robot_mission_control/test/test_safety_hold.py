"""HELD -- safety-стоп вытесняет всё (design §5.4 правило 5, §5.7, §9.2).

Реальные топики супервизора из реконсиляции §0.5: `/supervisor/estop`
(`std_msgs/Bool`) и `/supervisor/state` (`std_msgs/String`), не выдуманный
`/system/events`. Мостовая фраза при возобновлении после долгого простоя
(`held_resume_reannounce_s`) не реализована в шаге 7 -- см. докстринг
`fsm/states/held.py`; здесь проверяется сама механика HELD/CLEARED/
hold_timeout, не озвучка.
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import MissionState
from std_msgs.msg import Bool

from test.mission_fsm_test_helpers import (
    make_fsm_node,
    make_narration_node,
    make_run_tour_client,
    pump_clock,
    setup_single_stop_tour,
    speaking_started_count,
    state_is,
    state_listener,
)
from test.mocks.harness import MissionTestHarness, wait_for_future, wait_until


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _start_single_stop_tour_narrating(harness: MissionTestHarness, **fsm_overrides: object):
    setup_single_stop_tour(harness)
    make_narration_node(harness, lookahead=0)
    make_fsm_node(harness, nav_stop_timeout_s=3.0, **fsm_overrides)
    harness.say.chars_per_sec = 10.0

    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)
    speaking_count = speaking_started_count(client_node)
    # Публикатор заводится ЗАРАНЕЕ, до отправки RunTour: DDS-обнаружение
    # между свежесозданным publisher-ом и уже существующей подпиской
    # mission_fsm занимает какое-то реальное время -- сообщение,
    # отправленное сразу после create_publisher(), рискует быть отброшено
    # ещё до того, как стороны нашли друг друга. Долгая пауза на
    # nav+narrate ниже -- достаточное окно для этого "рукопожатия".
    estop_pub = client_node.create_publisher(Bool, "/supervisor/estop", 10)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=["lab105a"],
            greet=False,
            narrate=True,
            confirm_between_stops=False,
            return_home=True,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    pump_clock(
        harness,
        state_is(state, MissionState.STATE_NARRATING),
        step=harness.nav.duration_s + 0.05,
    )
    # Дождаться, что чанк 0 реально начал звучать -- иначе safety-событие
    # может прилететь раньше, чем NarratingState успеет узнать goal_handle
    # своего Narrate. Мок реагирует на это почти мгновенно, а у mission_fsm
    # свой шаг поллинга (poll_period_s, дефолт 20 мс) -- лишние 50 мс
    # реального времени гарантируют, что она уже забрала goal_handle.
    wait_until(lambda: speaking_count[0] >= 1, timeout_s=15.0)
    time.sleep(0.05)
    return client_node, state, goal_handle, estop_pub


def test_safety_hold_during_narrating_hard_stops_and_resumes(harness: MissionTestHarness) -> None:
    """SAFETY_HOLD в NARRATING -> жёсткий стоп + токен; SAFETY_CLEAR -> возобновление."""
    _client_node, state, goal_handle, estop_pub = _start_single_stop_tour_narrating(
        harness, held_max_s=100.0
    )
    epoch_before = harness.say.epoch

    estop_pub.publish(Bool(data=True))

    wait_until(state_is(state, MissionState.STATE_HELD), timeout_s=15.0)
    # STATE_HELD публикуется синхронно из _poll_loop сразу после того, как
    # cancel_active_work() ЗАПУСТИЛ cancel_goal_async() -- сам возврат HELD
    # не ждёт результата этого future. Реальный жёсткий стоп (RPC до
    # narration_server -> _run_plan замечает is_cancel_requested ->
    # _do_hard_stop публикует CancelAll -> mock_say_server поднимает epoch)
    # завершается чуть позже, асинхронно -- дождаться его отдельно.
    wait_until(lambda: harness.say.epoch > epoch_before, timeout_s=5.0)
    resume_token_at_hold = state["latest"].resume_token
    assert resume_token_at_hold != ""

    estop_pub.publish(Bool(data=False))
    wait_until(state_is(state, MissionState.STATE_NARRATING), timeout_s=15.0)
    assert state["latest"].resume_token == resume_token_at_hold

    harness.say.chars_per_sec = 50.0
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.2, max_iterations=100)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result
    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED


def test_held_max_s_forces_returning(harness: MissionTestHarness) -> None:
    """held_max_s -- если SAFETY_CLEAR так и не пришёл, HELD отпускает тур в RETURNING.

    Пока estop остаётся включён, RETURNING немедленно ловит тот же
    safety_hold_event на первом же поллинге (design правило 5 применяется
    ко ВСЕМ состояниям кроме HELD) и тут же отскакивает обратно в HELD --
    RETURNING->HELD в этом сценарии происходят синхронно, без единого
    `time.sleep()` между публикациями /mission/state, а у топика QoS
    depth=1 (design §7): поймать в подписчике именно кадр RETURNING
    ненадёжно -- это гонка на уровне DDS-очереди писателя, а не баг FSM.
    Вместо этого проверяем накопленное число переходов -- без работающего
    hold_timeout FSM намертво стояла бы в одном-единственном HELD.
    """
    _client_node, state, _goal_handle, estop_pub = _start_single_stop_tour_narrating(
        harness, held_max_s=0.1
    )

    estop_pub.publish(Bool(data=True))
    wait_until(state_is(state, MissionState.STATE_HELD), timeout_s=15.0)

    count_before = state["count"]
    for _ in range(6):
        harness.clock.advance(0.15)  # > held_max_s=0.1 -- каждый прыжок должен спровоцировать цикл
        time.sleep(0.03)

    assert state["count"] > count_before + 2


def test_safety_hold_pushed_frame_survives_during_confirm(harness: MissionTestHarness) -> None:
    """Правило 5: HELD посреди AWAITING_CONFIRM не трогает confirm-фрейм -- резюме идёт по нему.

    Нужны минимум 2 остановки: у тура из одной AWAITING_CONFIRM недостижим
    в принципе -- NarratingState на единственной remaining-stop сразу
    отдаёт TOUR_FINISHED, а не SUCCEEDED (см. fsm/states/narrating.py).
    """
    for i, stop_id in enumerate(("lab105a", "lab106")):
        harness.fixtures.add_exhibit(stop_id, ["Раз.", "Два."], version="rev1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = 0.05
    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(
        harness, nav_stop_timeout_s=3.0, confirm_timeout_s=100.0, held_max_s=100.0
    )
    harness.say.chars_per_sec = 50.0

    client_node, run_tour_client = make_run_tour_client(harness)
    state = state_listener(client_node)
    estop_pub = client_node.create_publisher(Bool, "/supervisor/estop", 10)

    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=["lab105a", "lab106"],
            greet=False,
            narrate=True,
            confirm_between_stops=True,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    pump_clock(
        harness,
        state_is(state, MissionState.STATE_AWAITING_CONFIRM),
        step=harness.nav.duration_s + 0.02,
        max_iterations=200,
    )

    estop_pub.publish(Bool(data=True))
    wait_until(state_is(state, MissionState.STATE_HELD), timeout_s=15.0)

    estop_pub.publish(Bool(data=False))
    # Резюме обязано вернуть именно в awaiting_confirm (через живой confirm-фрейм),
    # а не в navigating/narrating -- фрейм не был снят при входе в HELD.
    wait_until(state_is(state, MissionState.STATE_AWAITING_CONFIRM), timeout_s=15.0)

    fsm_node.submit_confirm(is_yes=False)  # закончить тур, не тащить дальше
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=100)
    wait_for_future(result_future, timeout_s=15.0)
    assert result_future.result().result.outcome == RunTour.Result.OUTCOME_COMPLETED
