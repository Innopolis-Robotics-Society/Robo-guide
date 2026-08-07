"""InterruptStack: механика слота глубины 1 (design §5.4) + FSM-уровень (design §12 п.6).

Первая часть файла -- чистая механика самого стека, без ROS (правила
1/2/3/4/6 из design §5.4). Вторая часть (после `# -- FSM-уровень --`) --
те же правила, но через реальный `mission_fsm_node` поверх реального
`narration_server_node` и моков: подтверждает, что стек, вызванный из
`fsm/states/answering.py` под настоящим `MultiThreadedExecutor`, ведёт себя
так же, как предсказывает чистая логика выше.

Кейсы design §9.2 про AskUser -> REJECTED ("по одному") и safety ->
PREEMPTED с восстановлением фрейма требуют состояний AWAITING_CONFIRM/HELD
-- это test_safety_hold.py (и частично test_tour_flow.py), не этот файл.
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.action import RunTour
from guide_robot_msgs.msg import CancelAll, MissionState

from guide_robot_mission_control.interrupt_stack import Frame, InterruptStack, StackBusyError
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


def test_push_answer_on_empty_stack_succeeds() -> None:
    stack = InterruptStack()
    frame = stack.push_answer(base_state="NARRATING", resume_token="tok", now=10.0)
    assert stack.is_busy()
    assert stack.frame == frame
    assert frame.kind == "answer"
    assert frame.deadline is None


def test_push_confirm_on_empty_stack_succeeds() -> None:
    stack = InterruptStack()
    frame = stack.push_confirm(base_state="NAVIGATING", resume_token="", now=5.0, deadline=25.0)
    assert frame.kind == "confirm"
    assert frame.deadline == 25.0


def test_push_answer_on_busy_stack_raises_and_does_not_mutate() -> None:
    """Правило 2: AskUser поверх занятого стека -- явный отказ, фрейм не меняется."""
    stack = InterruptStack()
    original = stack.push_answer(base_state="NARRATING", resume_token="tok", now=1.0)
    with pytest.raises(StackBusyError):
        stack.push_answer(base_state="NAVIGATING", resume_token="other", now=2.0)
    assert stack.frame == original  # не подменился


def test_push_confirm_on_busy_stack_raises_and_does_not_mutate() -> None:
    stack = InterruptStack()
    original = stack.push_confirm(base_state="NAVIGATING", resume_token="", now=1.0, deadline=20.0)
    with pytest.raises(StackBusyError):
        stack.push_confirm(base_state="NARRATING", resume_token="x", now=2.0, deadline=99.0)
    assert stack.frame == original


def test_barge_in_over_answer_frame_reuses_it_depth_stays_one() -> None:
    """Правило 3: barge-in поверх answer -- фрейм переиспользуется, не пушится второй."""
    stack = InterruptStack()
    stack.push_answer(base_state="NARRATING", resume_token="tok", now=1.0)
    reused = stack.on_barge_in(now=7.5)
    assert stack.is_busy()
    assert reused.kind == "answer"
    assert reused.base_state == "NARRATING"
    assert reused.resume_token == "tok"
    assert reused.opened_at == 7.5  # сдвинулось
    assert reused.deadline is None


def test_barge_in_over_confirm_frame_replaces_with_answer() -> None:
    """Правило 4: barge-in поверх confirm -- он заменяется на answer, тот же base_state/token."""
    stack = InterruptStack()
    stack.push_confirm(base_state="NAVIGATING", resume_token="rt", now=1.0, deadline=21.0)
    replaced = stack.on_barge_in(now=3.0)
    assert replaced.kind == "answer"
    assert replaced.base_state == "NAVIGATING"
    assert replaced.resume_token == "rt"
    assert replaced.deadline is None
    assert stack.frame == replaced  # глубина всё ещё 1


def test_barge_in_on_empty_stack_raises() -> None:
    stack = InterruptStack()
    with pytest.raises(StackBusyError):
        stack.on_barge_in(now=1.0)


def test_pop_frees_the_slot_and_returns_prior_frame() -> None:
    stack = InterruptStack()
    frame = stack.push_answer(base_state="NARRATING", resume_token="tok", now=1.0)
    popped = stack.pop()
    assert popped == frame
    assert not stack.is_busy()
    assert stack.frame is None


def test_pop_on_empty_stack_returns_none() -> None:
    stack = InterruptStack()
    assert stack.pop() is None


def test_answer_max_s_forced_pop() -> None:
    """Правило 6: answer_max_s -- принудительный pop зависшего answer-фрейма."""
    stack = InterruptStack()
    stack.push_answer(base_state="NARRATING", resume_token="tok", now=100.0)
    assert not stack.answer_timed_out(now=130.0, answer_max_s=45.0)
    assert stack.answer_timed_out(now=146.0, answer_max_s=45.0)
    frame = stack.pop()
    assert frame is not None and frame.kind == "answer"


def test_answer_timeout_check_ignores_confirm_frame() -> None:
    """answer_max_s -- защита только для answer; у confirm свой deadline."""
    stack = InterruptStack()
    stack.push_confirm(base_state="NAVIGATING", resume_token="", now=0.0, deadline=20.0)
    assert not stack.answer_timed_out(now=1000.0, answer_max_s=45.0)


def test_safety_hold_does_not_touch_the_frame() -> None:
    """Правило 5: HELD -- не фрейм, стек просто не трогается mission_fsm-ом.

    Сам InterruptStack ничего не знает про safety -- проверяем только, что
    ничего не мешает фрейму просто оставаться на месте, пока вызывающий код
    его не трогает (восстановление после SAFETY_CLEAR -- поведение
    mission_fsm, не стека).
    """
    stack = InterruptStack()
    frame = stack.push_answer(base_state="NARRATING", resume_token="tok", now=1.0)
    # ... simulated SAFETY_HOLD/SAFETY_CLEAR happens entirely outside the stack ...
    assert stack.frame == frame


def test_frame_is_immutable() -> None:
    frame = Frame(
        kind="answer", base_state="NARRATING", resume_token="tok", opened_at=1.0, deadline=None
    )
    with pytest.raises(AttributeError):
        frame.opened_at = 2.0  # type: ignore[misc]


# -- FSM-уровень: mission_fsm + narration_server + моки ----------------------


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _start_tour_to_narrating(harness: MissionTestHarness, *, answer_max_s: float = 5.0):
    """RunTour(greet=False, confirm_between_stops=False, return_home=False) на одну остановку.

    Возвращает (client_node, state, goal_handle, fsm_node) уже в момент,
    когда /mission/state впервые показал NARRATING.
    """
    setup_single_stop_tour(harness)
    make_narration_node(harness, lookahead=0)
    fsm_node = make_fsm_node(harness, answer_max_s=answer_max_s, nav_stop_timeout_s=5.0)
    harness.say.chars_per_sec = 10.0

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

    pump_clock(
        harness,
        state_is(state, MissionState.STATE_NARRATING),
        step=harness.nav.duration_s + 0.05,
    )
    time.sleep(0.05)  # дать narration_server реально отправить Say для чанка 0
    return client_node, state, goal_handle, fsm_node


def test_narrating_completes_without_interruption_returns_to_idle(
    harness: MissionTestHarness,
) -> None:
    _client_node, _state, goal_handle, _fsm = _start_tour_to_narrating(harness)

    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.5)
    wait_for_future(result_future, timeout_s=15.0)
    result: RunTour.Result = result_future.result().result

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert result.stops_completed == 1


def test_barge_in_during_narrating_enters_answering_then_resumes_on_timeout(
    harness: MissionTestHarness,
) -> None:
    """Design §5.2/§5.4: interrupted -> ANSWERING; answer_max_s -> resume_base -> NARRATING."""
    client_node, state, goal_handle, _fsm = _start_tour_to_narrating(harness, answer_max_s=0.2)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)

    cancel_all_pub.publish(
        CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
    )

    wait_until(
        state_is(state, MissionState.STATE_ANSWERING),
        timeout_s=15.0,
    )
    assert state["latest"].interrupt == MissionState.IRQ_ANSWERING
    resume_token_at_interrupt = state["latest"].resume_token
    assert resume_token_at_interrupt != ""

    harness.clock.advance(0.3)  # > answer_max_s=0.2 -- принудительный pop (правило 6)
    wait_until(
        state_is(state, MissionState.STATE_NARRATING),
        timeout_s=15.0,
    )
    assert state["latest"].resume_token == resume_token_at_interrupt

    harness.say.chars_per_sec = 50.0
    result_future = goal_handle.get_result_async()
    pump_clock(harness, result_future.done, step=0.5)
    wait_for_future(result_future, timeout_s=15.0)
    assert result_future.result().result.outcome == RunTour.Result.OUTCOME_COMPLETED


def test_repeated_barge_in_during_answering_reuses_frame_stays_in_answering(
    harness: MissionTestHarness,
) -> None:
    """Правило 3: повторный barge-in поверх answer не роняет FSM (двойной push_answer)."""
    client_node, state, _goal_handle, _fsm = _start_tour_to_narrating(harness, answer_max_s=5.0)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)

    cancel_all_pub.publish(
        CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
    )
    wait_until(
        state_is(state, MissionState.STATE_ANSWERING),
        timeout_s=15.0,
    )

    for _ in range(3):
        time.sleep(0.05)
        cancel_all_pub.publish(
            CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
        )
        time.sleep(0.05)
        assert state["latest"].state == MissionState.STATE_ANSWERING

    # answer_max_s всё ещё работает штатно после серии barge-in -- если бы
    # StackBusyError уронил execute_callback тихо, этот финальный переход
    # никогда бы не случился.
    harness.clock.advance(5.2)
    wait_until(
        state_is(state, MissionState.STATE_NARRATING),
        timeout_s=15.0,
    )


def test_submit_answer_during_answering_resumes_promptly(harness: MissionTestHarness) -> None:
    """ANSWERED -- отдельный от TIMEOUT путь (design §5.2), проверяется через answer_max_s."""
    client_node, state, _goal_handle, fsm = _start_tour_to_narrating(harness, answer_max_s=100.0)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)

    cancel_all_pub.publish(
        CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
    )
    wait_until(
        state_is(state, MissionState.STATE_ANSWERING),
        timeout_s=15.0,
    )

    fsm.submit_answer("да, продолжайте")
    # timeout_s здесь << answer_max_s=100.0 -- если бы сработал TIMEOUT, а
    # не ANSWERED, часы вообще не двигались, и это бы просто зависло.
    wait_until(
        state_is(state, MissionState.STATE_NARRATING),
        timeout_s=5.0,
    )
