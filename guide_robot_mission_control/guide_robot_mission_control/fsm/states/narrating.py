"""NARRATING (design §5.2, §5.5): один `Narrate` goal на текущую остановку тура.

На barge-in `narration_server` сам обрывает свой `Narrate` (design §4.2,
"слушает сам, не через FSM") -- это состояние barge-in не отменяет
явно, только ждёт результата, который придёт с `OUTCOME_INTERRUPTED`.

`PAUSED` (design §6/§8: presence) в шаге 7 -- тестовый хук
(`FsmContext.request_pause()`), не живая интеграция с `/mission/presence`
-- см. докстринг `fsm/states/paused.py`.

`tour.narrate=False` (design §2.4: "только объезд, для проверки
навигации") -- goal `Narrate` не отправляется вовсе, состояние сразу
продвигает тур тем же кодом, что и после реального завершения рассказа.
"""

from __future__ import annotations

import time

from guide_robot_msgs.action import Narrate

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["NarratingState"]


class NarratingState(InterruptibleState):
    """Отправляет `Narrate`, ждёт результата -- сама его не отменяет (см. докстринг модуля)."""

    name = "narrating"

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить Narrate-goal на текущий экспонат -- пропустить, если tour.narrate=False."""
        self._skip = not blackboard.tour.narrate
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        if self._skip:
            return
        goal = Narrate.Goal(
            exhibit_id=blackboard.tour.current_exhibit_id, resume_token=blackboard.resume_token
        )
        self._send_future = self.ctx.narrate_client.send_goal_async(goal)

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться принятия goal-а, затем результата; параллельно следить за паузой."""
        del now_ns
        if self._skip:
            return self._advance(blackboard)
        if self.ctx.take_pause_request():
            # Как и при CANCELED/HELD -- активный Narrate обязан быть
            # остановлен здесь, а не просто брошен. Иначе narration_server
            # (self._active_execution) остаётся занят ЭТИМ goal-ом; после
            # resume NarratingState.on_enter() шлёт НОВЫЙ Narrate -- и
            # получает OUTCOME_REJECTED("busy") на пустом месте, что через
            # _skip_stop пропускает остановку и завершает тур досрочно
            # (воспроизведено вживую: pause -> resume -> тихо домой).
            self.cancel_active_work(blackboard, outcomes.PAUSED)
            return outcomes.PAUSED
        if self._goal_handle is None:
            return self._poll_send(blackboard)
        return self._poll_result(blackboard)

    def _poll_send(self, blackboard: Blackboard) -> str | None:
        if not self._send_future.done():  # type: ignore[attr-defined]
            return None
        self._goal_handle = self._send_future.result()  # type: ignore[attr-defined]
        blackboard.narrate_goal_handle = self._goal_handle
        if not self._goal_handle.accepted:  # type: ignore[attr-defined]
            return self._skip_stop(blackboard, "Narrate goal не принят")
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self, blackboard: Blackboard) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        result: Narrate.Result = self._result_future.result().result  # type: ignore[attr-defined]
        blackboard.resume_token = result.resume_token
        if result.outcome == Narrate.Result.OUTCOME_COMPLETED:
            return self._advance(blackboard)
        if result.outcome == Narrate.Result.OUTCOME_INTERRUPTED:
            self.ctx.consume_barge_in()
            return outcomes.INTERRUPTED
        # OUTCOME_ABORTED/OUTCOME_REJECTED (design: "занят другим goal /
        # контент не найден") -- контент для остановки может отсутствовать
        # (плохие/неполные данные semantic_map), это не должно ронять
        # весь RunTour необработанным исходом (было: `_TRANSITIONS`
        # не знал про ABORTED -> RuntimeError, воспроизведено вживую).
        # Пропускаем остановку тем же путём, что и NAV_FAILED.
        reason = f"Narrate outcome={result.outcome} detail={result.detail!r}"
        return self._skip_stop(blackboard, reason)

    def _advance(self, blackboard: Blackboard) -> str:
        blackboard.stops_completed += 1
        if not blackboard.tour.has_next_stop:
            return outcomes.TOUR_FINISHED
        blackboard.tour.index += 1
        blackboard.resume_token = ""
        return outcomes.SUCCEEDED

    def _skip_stop(self, blackboard: Blackboard, reason: str) -> str:
        self.ctx.log(
            f"narrating: пропускаю остановку {blackboard.tour.current_stop_id!r} ({reason})"
        )
        blackboard.stops_skipped += 1
        if not blackboard.tour.has_next_stop:
            return outcomes.TOUR_FINISHED
        blackboard.tour.index += 1
        blackboard.resume_token = ""
        return outcomes.NARRATE_FAILED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD/PAUSED -- жёстко остановить активный Narrate (design §5.7: MODE_HARD).

        PAUSED вызывает это САМА (см. `poll()`) -- в отличие от CANCELED/HELD,
        её не производит база (`fsm/base.py`), она приходит из собственного
        `poll()` этого состояния, и без явного вызова здесь narration_server
        остался бы занят брошенным goal-ом (design §5.7 упоминает только
        MODE_SOFT для presence-паузы; полноценная интеграция с
        `NarrationControl.srv` отложена вместе с `/mission/presence` -- см.
        докстринг `fsm/states/paused.py`, здесь временно тот же MODE_HARD,
        что и у CANCELED/HELD, лишь бы не терять активный goal).

        `cancel_goal_async()` только запускает отмену -- сам возврат
        HELD/CANCELED из `_poll_loop` синхронный и не ждёт её результата.
        Но именно в результате `Narrate` (OUTCOME_INTERRUPTED) лежит
        актуальный `resume_token` (design §3, §4.2), а верхняя SM кладёт в
        `/mission/state` то, что есть в блэкборде НА МОМЕНТ входа в
        следующее состояние -- если не дождаться здесь, HELD/PAUSED
        опубликуется со СТАРЫМ токеном. Ждём ограниченно
        (`hard_stop_result_timeout_s`, design §4.2: тот же бюджет, которым
        сам narration_server ограничивает свой `_do_hard_stop`) реальными
        миллисекундами -- не `spin_until_future_complete` (см. докстринг
        base.py), а тот же sleep-поллинг, что и everywhere в FSM.
        """
        if self._goal_handle is None or self._result_future is None:
            return
        self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
        deadline = self.ctx.now_ns() + int(self.ctx.hard_stop_result_timeout_s * 1e9)
        while self.ctx.now_ns() < deadline:
            if self._result_future.done():  # type: ignore[attr-defined]
                self._capture_resume_token(blackboard)
                return
            time.sleep(self.ctx.poll_period_s)
        if self._result_future.done():  # type: ignore[attr-defined]
            self._capture_resume_token(blackboard)

    def _capture_resume_token(self, blackboard: Blackboard) -> None:
        result: Narrate.Result = self._result_future.result().result  # type: ignore[attr-defined]
        blackboard.resume_token = result.resume_token

    def on_exit(self, blackboard: Blackboard, outcome: str) -> None:
        """Снять goal_handle с блэкборда -- он больше не в полёте."""
        del outcome
        blackboard.narrate_goal_handle = None
