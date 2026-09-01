"""NAVIGATING (design §5.2, §5.5): `NavigateToPose` на текущую остановку тура.

`nav_failed` (таймаут или ABORT) НЕ абортит тур (design §5.5) -- пропускает
остановку (`stops_skipped++`) и сразу продвигает `tour.index`, возвращая
либо `NAV_FAILED` (едем к следующей), либо `TOUR_FINISHED` (остановок
больше нет). Полноценный диалог "рассказать отсюда / пропустить" из §5.5
требует того же недостающего ASR/LLM-матчинга, что и AWAITING_CONFIRM --
в шаге 7 сознательно не реализован, дефолт `on_timeout=ASSUME_DEFAULT`
("пропустить") применяется напрямую, без вопроса.

Транзитный нарратив на ходу (design §5.6, stage2 блок E): не более
одного `Narrate`-чанка за ход навигации, только если ход длится дольше
`transit_after_s` и речь сейчас не идёт (`ctx.is_speaking()`). Чанк идёт
с `text=` уже готовым (взят из `blackboard.tour.transit_chunks`,
пополненных ОДИН раз на весь тур в `mission_fsm_node._execute_run_tour`)
-- `narration_server._resolve_content()` тогда не лезет за
`GetExhibitContent` сам и строит план из ОДНОГО чанка, который
завершается `OUTCOME_COMPLETED` без резюме сам по себе: `continuity`/
`priority`/`scope` полей `Narrate.Goal` narration_server сегодня НЕ
читает вовсе (говорит всегда своими параметрами узла
`say_priority`/`say_scope`, по умолчанию как раз `PRIORITY_NARRATION`/
`SCOPE_NARRATION`) -- отсутствие резюме гарантирует не поле `continuity`,
а то, что этот `resume_token` просто никогда никем не переиспользуется.
Fire-and-forget на ходу: FSM не ждёт результата и не гейтит на нём
навигацию. На выходе из состояния (прибытие / skip / cancel) активный
транзитный `Narrate` обязан быть снят -- иначе `narration_server`
(`_active_execution`) остаётся занят DROPPABLE-goal-ом, а
`NarratingState` на остановке получает `OUTCOME_REJECTED("busy")` и
через `_skip_stop` молча едет дальше (воспроизведено вживую: Q&A →
`lab_demo` → тихий объезд точек). Design §5.6 хочет `MODE_SOFT` на
прибытии; FSM `NarrationControl` не вызывает (см. README), поэтому
здесь тот же `cancel_goal_async`, что и у `NarratingState`.

`hold_position` (stage2 D3): `FsmContext.take_pause_request()` (тот же
примитив, что и у `NarratingState`) отменяет активный `NavigateToPose` и
уводит в PAUSED. Специального "сохранения цели" не нужно --
`blackboard.tour.index` не двигается, поэтому `RESUMED -> resume_base ->
navigating` (root_sm._resume_target через `interrupted_from`, т.к. у
NAVIGATING нет фрейма стека) просто заново шлёт `NavigateToPose` на ТУ ЖЕ
остановку из свежего `on_enter()`.
"""

from __future__ import annotations

import time

from action_msgs.msg import GoalStatus
from guide_robot_msgs.action import Narrate, Say
from nav2_msgs.action import NavigateToPose

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["NavigatingState"]


class NavigatingState(InterruptibleState):
    """Отправляет `NavigateToPose` на позу текущей остановки, следит за таймаутом."""

    name = "navigating"
    redirect_eligible = True

    def on_enter(self, blackboard: Blackboard) -> None:
        """Отправить NavigateToPose на позу текущей остановки тура."""
        goal = NavigateToPose.Goal()
        goal.pose = self.ctx.resolve_pose(blackboard.tour.current_stop_id)
        self._send_future = self.ctx.nav_client.send_goal_async(goal)
        self._goal_handle: object | None = None
        self._result_future: object | None = None
        self._start_ns = self.ctx.now_ns()
        self._transit_fired = False
        self._transit_send_future: object | None = None
        self._transit_handle: object | None = None
        self._transit_result_future: object | None = None

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Дождаться принятия goal-а, затем результата, следя за nav_stop_timeout_s."""
        if self.ctx.take_pause_request():
            # stage2 D3: hold_position -- остановиться на месте, не отменяя
            # тур. Как и у CANCELED/HELD, активная работа обязана быть
            # остановлена здесь же, до выдачи исхода.
            blackboard.pause_reason = "user"
            self.cancel_active_work(blackboard, outcomes.PAUSED)
            return outcomes.PAUSED
        self._maybe_fire_transit(blackboard, now_ns)
        self._bind_transit()
        if self._goal_handle is None:
            return self._poll_send(blackboard)
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s >= self.ctx.nav_stop_timeout_s:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]
            reason = f"nav_stop_timeout_s={self.ctx.nav_stop_timeout_s} истёк"
            return self._skip_stop(blackboard, reason)
        return self._poll_result(blackboard)

    def _maybe_fire_transit(self, blackboard: Blackboard, now_ns: int) -> None:
        """Сказать один транзитный чанк, если ход достаточно долгий (stage2 блок E).

        Не более одного чанка за ЭТОТ ход (`self._transit_fired`) и не
        более `len(transit_chunks)` за весь тур (`transit_next_index`) --
        "без повторов за тур". `is_speaking()` -- не мешать барж-ину/уже
        идущей речи; барж-ин поверх УЖЕ звучащего транзитного чанка и так
        перебивает его через общий `/speech/cancel_all` narration_server-а,
        сюда это никак не сигналится и не должно -- FSM его не ждёт.
        """
        if self._transit_fired:
            return
        if blackboard.tour.transit_next_index >= len(blackboard.tour.transit_chunks):
            return
        elapsed_s = (now_ns - self._start_ns) / 1e9
        if elapsed_s < self.ctx.transit_after_s or self.ctx.is_speaking():
            return
        text = blackboard.tour.transit_chunks[blackboard.tour.transit_next_index]
        blackboard.tour.transit_next_index += 1
        self._transit_fired = True
        # priority/scope -- значения из Say.Goal (Narrate.Goal их не
        # объявляет сама, rosidl не даёт делить константы между файлами;
        # см. Narrate.action). narration_server сегодня их не читает вовсе
        # (свои say_priority/say_scope, по умолчанию те же самые) -- поля
        # заполнены на будущее и для ясности намерения, не для эффекта.
        goal = Narrate.Goal(
            exhibit_id=blackboard.tour.transit_content_id,
            text=text,
            priority=Say.Goal.PRIORITY_NARRATION,
            scope=Say.Goal.SCOPE_NARRATION,
            continuity=Narrate.Goal.CONTINUITY_DROPPABLE,
        )
        self._transit_send_future = self.ctx.narrate_client.send_goal_async(goal)

    def _bind_transit(self) -> None:
        """Забрать handle транзитного Narrate, как только send_goal ответит."""
        if self._transit_handle is not None or self._transit_send_future is None:
            return
        if not self._transit_send_future.done():  # type: ignore[attr-defined]
            return
        handle = self._transit_send_future.result()  # type: ignore[attr-defined]
        if handle is None or not handle.accepted:  # type: ignore[attr-defined]
            return
        self._transit_handle = handle
        self._transit_result_future = handle.get_result_async()  # type: ignore[attr-defined]

    def _stop_transit(self) -> None:
        """Снять транзитный Narrate, чтобы слот narration_server освободился.

        `cancel_goal_async()` только запускает отмену -- без ожидания
        результата `_active_execution` ещё занят, и следующий
        `NarratingState.on_enter()` ловит busy. Ждём ограниченно
        `hard_stop_result_timeout_s`, как `NarratingState.cancel_active_work`.
        """
        if self._transit_send_future is None:
            return
        deadline = self.ctx.now_ns() + int(self.ctx.hard_stop_result_timeout_s * 1e9)
        while self.ctx.now_ns() < deadline:
            if self._transit_send_future.done():  # type: ignore[attr-defined]
                break
            time.sleep(self.ctx.poll_period_s)
        self._bind_transit()
        if self._transit_handle is None or self._transit_result_future is None:
            return
        if self._transit_result_future.done():  # type: ignore[attr-defined]
            return
        self._transit_handle.cancel_goal_async()  # type: ignore[attr-defined]
        deadline = self.ctx.now_ns() + int(self.ctx.hard_stop_result_timeout_s * 1e9)
        while self.ctx.now_ns() < deadline:
            if self._transit_result_future.done():  # type: ignore[attr-defined]
                return
            time.sleep(self.ctx.poll_period_s)

    def _poll_send(self, blackboard: Blackboard) -> str | None:
        if not self._send_future.done():  # type: ignore[attr-defined]
            return None
        self._goal_handle = self._send_future.result()  # type: ignore[attr-defined]
        if not self._goal_handle.accepted:  # type: ignore[attr-defined]
            return self._skip_stop(blackboard, "NavigateToPose goal не принят")
        self._result_future = self._goal_handle.get_result_async()  # type: ignore[attr-defined]
        return None

    def _poll_result(self, blackboard: Blackboard) -> str | None:
        if not self._result_future.done():  # type: ignore[attr-defined]
            return None
        status = self._result_future.result().status  # type: ignore[attr-defined]
        if status == GoalStatus.STATUS_SUCCEEDED:
            return outcomes.ARRIVED
        return self._skip_stop(blackboard, f"NavigateToPose status={status}")

    def _skip_stop(self, blackboard: Blackboard, reason: str) -> str:
        self.ctx.log(
            f"navigating: пропускаю остановку {blackboard.tour.current_stop_id!r} ({reason})"
        )
        blackboard.stops_skipped += 1
        if not blackboard.tour.has_next_stop:
            return outcomes.TOUR_FINISHED
        blackboard.tour.index += 1
        return outcomes.NAV_FAILED

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """CANCELED/HELD -- отменить активный NavigateToPose (транзит снимет on_exit)."""
        del blackboard, outcome
        if self._goal_handle is not None and self._result_future is not None:
            self._goal_handle.cancel_goal_async()  # type: ignore[attr-defined]

    def on_exit(self, blackboard: Blackboard, outcome: str) -> None:
        """Освободить слот Narrate до входа в NARRATING/PAUSED/HELD."""
        del blackboard, outcome
        self._stop_transit()
