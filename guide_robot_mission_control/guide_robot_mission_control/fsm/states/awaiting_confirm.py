"""AWAITING_CONFIRM (design §5.2, §5.4 правило 4): «Идём дальше?» между остановками.

Реальный матчинг ответа по `option_phrases` (design §2.5 `AskUser.action`)
требует ASR/LLM-связки, которой в v1 нет -- как и `ANSWERED` у ANSWERING,
ответ здесь приходит через тестовый хук `FsmContext.submit_confirm()`.

Голосом ответ приходит из `tool_broker` (fast-path «да/давай/нет») или из
`dialog_agent` (ЛЛМ, если fast-path не уверен) -- оба зовут тот же
`~/submit_confirm`. При wake-гейте они слышат посетителя только в открытом
окне: вопрос задаёт не ЛЛМ-ход, `DialogTurnEvent.expects_reply` его окно не
откроет. Поэтому договорённый до конца вопрос (`Say` COMPLETED -- tts_node
отдаёт его после `wait_idle`, то есть звук уже доигран) сам зовёт
`ctx.prompt_listen()` -- тот же путь, что кнопка «Задать вопрос» на экране.

Правило 4: barge-in поверх confirm-фрейма заменяет его на answer, вопрос
после этого задаётся заново (design: "stage=REPEATING"), не больше
`confirm_repeat_max` раз (дефолт 1) -- по исчерпании лимита дальнейшее
поведение здесь совпадает с истечением таймера (см. `_give_up`).

Дефолт по таймауту -- `NO` (закончить тур), не `YES`: design не
специфицирует политику для этого конкретного вопроса (`on_timeout` из
§2.5 -- поле per-question `AskUser.Goal`, которого нет у общего
`RunTour.confirm_between_stops`), консервативный выбор -- не тащить
дальше не отвечающего посетителя.
"""

from __future__ import annotations

from guide_robot_msgs.action import Say

from guide_robot_mission_control.fsm import outcomes
from guide_robot_mission_control.fsm.base import InterruptibleState
from guide_robot_mission_control.fsm.blackboard_keys import Blackboard

__all__ = ["AwaitingConfirmState"]


class AwaitingConfirmState(InterruptibleState):
    """Держит confirm-фрейм, переспрашивает на barge-in, таймаут/лимит повторов -> NO."""

    name = "awaiting_confirm"
    redirect_eligible = True

    def on_enter(self, blackboard: Blackboard) -> None:
        """Задать вопрос первый раз -- либо продолжить с уже открытого фрейма.

        Единственное состояние, чей фрейм резюмируется В НЕГО ЖЕ САМОГО:
        `base_state=self.name` (см. `_ask`). Правило 5 (HELD не трогает
        confirm-фрейм) значит, что RESUME_BASE после HELD посреди
        AWAITING_CONFIRM возвращает сюда с фреймом, который САМ ещё не
        снят -- повторный `push_confirm()` в этом случае падает в
        `StackBusyError` на пустом месте. `_ask()` (заново) нужен только
        когда фрейма ещё нет.
        """
        self._repeat_count = 0
        self._say_send_future: object | None = None
        self._say_result_future: object | None = None
        frame = blackboard.stack.frame
        if frame is not None and frame.kind == "confirm" and frame.base_state == self.name:
            return
        self._ask(blackboard)

    def _ask(self, blackboard: Blackboard) -> None:
        now_s = self.ctx.now_ns() / 1e9
        blackboard.stack.push_confirm(
            base_state=self.name,
            resume_token=blackboard.resume_token,
            now=now_s,
            deadline=now_s + self.ctx.confirm_timeout_s,
        )
        goal = Say.Goal(
            text=self.ctx.confirm_question_text,
            scope=Say.Goal.SCOPE_DIALOG,
            priority=Say.Goal.PRIORITY_DIALOG,
            interruptible=True,
        )
        # Результат синтеза не блокирует poll(): ответ (экран, barge-in)
        # принимается и во время вопроса. Он нужен только чтобы открыть
        # окно ответа без имени, когда вопрос договорён (`_poll_question_spoken`).
        self._say_send_future = self.ctx.say_client.send_goal_async(goal)
        self._say_result_future = None

    def _poll_question_spoken(self) -> None:
        """Вопрос договорён целиком -- открыть окно ответа без имени.

        Прерванный (barge-in) или отклонённый вопрос окна не открывает:
        barge-in переспрашивает заново через `_ask`, и окно откроет уже
        новый вопрос.
        """
        if self._say_result_future is None:
            if self._say_send_future is None or not self._say_send_future.done():  # type: ignore[attr-defined]
                return
            handle = self._say_send_future.result()  # type: ignore[attr-defined]
            self._say_send_future = None
            if handle.accepted:  # type: ignore[attr-defined]
                self._say_result_future = handle.get_result_async()  # type: ignore[attr-defined]
            return
        if not self._say_result_future.done():  # type: ignore[attr-defined]
            return
        result: Say.Result = self._say_result_future.result().result  # type: ignore[attr-defined]
        self._say_result_future = None
        if result.status == Say.Result.STATUS_COMPLETED:
            self.ctx.prompt_listen()

    def poll(self, blackboard: Blackboard, now_ns: int) -> str | None:
        """Ждать ответ / повторный barge-in (правило 4) / истечение confirm_timeout_s."""
        now_s = now_ns / 1e9
        self._poll_question_spoken()

        answer = self.ctx.take_confirm_answer()
        if answer is not None:
            self.ctx.log(f"confirm: получен ответ -- {'да' if answer else 'нет'}")
            blackboard.stack.pop()
            return outcomes.YES if answer else outcomes.NO

        if self.ctx.take_question_request():
            # «Задать вопрос» с экрана: снять вопрос и его таймер, ответить в
            # ANSWERING; RESUME_BASE вернёт сюда (interrupted_from) и спросит снова.
            self.ctx.log("confirm: посетитель хочет задать вопрос -> ANSWERING")
            blackboard.stack.pop()
            return outcomes.INTERRUPTED

        if self.ctx.consume_barge_in():
            blackboard.stack.on_barge_in(now=now_s)  # confirm -> answer, design правило 4
            self._repeat_count += 1
            if self._repeat_count > self.ctx.confirm_repeat_max:
                reason = f"barge-in превысил confirm_repeat_max={self.ctx.confirm_repeat_max}"
                return self._give_up(blackboard, reason)
            self.ctx.log(f"confirm: barge-in, переспрашиваю (попытка {self._repeat_count})")
            blackboard.stack.pop()
            self._ask(blackboard)
            return None

        frame = blackboard.stack.frame
        if frame is not None and frame.kind == "confirm" and now_s >= frame.deadline:
            reason = f"confirm_timeout_s={self.ctx.confirm_timeout_s} истёк"
            return self._give_up(blackboard, reason)

        return None

    def _give_up(self, blackboard: Blackboard, reason: str) -> str:
        self.ctx.log(f"confirm: без ответа -> NO ({reason})")
        blackboard.stack.pop()
        return outcomes.NO

    def cancel_active_work(self, blackboard: Blackboard, outcome: str) -> None:
        """Снять фрейм при CANCELED/REDIRECTED; НЕ трогать при HELD (design правило 5).

        REDIRECTED (stage2 B2) -- как и CANCELED, confirm-фрейм больше не
        актуален: тур продолжается по совсем другому (одностоповому) плану.
        """
        if outcome in (outcomes.CANCELED, outcomes.REDIRECTED):
            blackboard.stack.pop()
