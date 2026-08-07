"""Стек прерываний глубины 1 (design §5.4).

Не структура данных "стек" в общем смысле -- ровно один слот. Глубина > 1
осознанно не делается в v1 (design §13): второй одновременный запрос на
прерывание получает явный отказ, а не встаёт в очередь.

Время передаётся снаружи как float-секунды (не rclpy.time.Time) -- модуль
не знает про ROS, вызывающий код (mission_fsm, на своих часах) сам решает,
что такое "сейчас".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

__all__ = ["Frame", "InterruptStack", "StackBusyError"]

FrameKind = Literal["answer", "confirm"]


class StackBusyError(Exception):
    """push_answer()/push_confirm() при уже занятом стеке.

    Вызывающий (mission_fsm) обязан превратить это в AskUser.OUTCOME_REJECTED
    и фразу phrases.one_at_a_time (design §5.4, правило 2) -- сам фрейм при
    этом не меняется, это отдельно проверяемый инвариант.
    """


@dataclass(frozen=True)
class Frame:
    """Один снятый с блэкборда кадр прерывания."""

    kind: FrameKind
    base_state: str
    resume_token: str
    opened_at: float
    # None для answer -- у него нет собственного дедлайна кроме answer_max_s.
    deadline: float | None


class InterruptStack:
    """Единственный слот прерывания плюс правила его занятия/освобождения."""

    def __init__(self) -> None:
        """Создать пустой (свободный) стек."""
        self._frame: Frame | None = None

    @property
    def frame(self) -> Frame | None:
        """Текущий фрейм, либо None, если стек свободен."""
        return self._frame

    def is_busy(self) -> bool:
        """Вернуть True, если слот занят."""
        return self._frame is not None

    def push_answer(self, *, base_state: str, resume_token: str, now: float) -> Frame:
        """Открыть фрейм ответа (правило 1/2). StackBusyError, если слот занят."""
        if self._frame is not None:
            raise StackBusyError("стек занят")
        frame = Frame(
            kind="answer",
            base_state=base_state,
            resume_token=resume_token,
            opened_at=now,
            deadline=None,
        )
        self._frame = frame
        return frame

    def push_confirm(
        self, *, base_state: str, resume_token: str, now: float, deadline: float
    ) -> Frame:
        """Открыть фрейм AWAITING_CONFIRM. StackBusyError, если слот занят."""
        if self._frame is not None:
            raise StackBusyError("стек занят")
        frame = Frame(
            kind="confirm",
            base_state=base_state,
            resume_token=resume_token,
            opened_at=now,
            deadline=deadline,
        )
        self._frame = frame
        return frame

    def on_barge_in(self, *, now: float) -> Frame:
        """Правило 3/4: barge-in поверх занятого слота переиспользует фрейм, не пушит новый.

        answer -> тот же фрейм, только opened_at сдвигается (пользователь
        переспросил, это не вложенность). confirm -> заменяется на answer с
        тем же base_state/resume_token (после ответа вопрос будет задан
        заново вызывающим кодом, deadline здесь ни при чём).

        Требует уже открытого фрейма: barge-in при пустом стеке -- отдельный
        путь на уровне FSM (переход в ANSWERING сам вызывает push_answer),
        сюда попадать не должен.
        """
        if self._frame is None:
            raise StackBusyError("barge-in без активного фрейма")
        if self._frame.kind == "answer":
            self._frame = replace(self._frame, opened_at=now)
        else:
            self._frame = Frame(
                kind="answer",
                base_state=self._frame.base_state,
                resume_token=self._frame.resume_token,
                opened_at=now,
                deadline=None,
            )
        return self._frame

    def pop(self) -> Frame | None:
        """Освободить слот и вернуть то, что в нём было (None, если было пусто)."""
        frame = self._frame
        self._frame = None
        return frame

    def answer_timed_out(self, *, now: float, answer_max_s: float) -> bool:
        """Правило 6: True, если открытый answer-фрейм превысил answer_max_s.

        Не снимает фрейм сама -- вызывающий код решает, когда именно
        (обычно сразу вызывает pop()) и с каким detail.
        """
        if self._frame is None or self._frame.kind != "answer":
            return False
        return (now - self._frame.opened_at) >= answer_max_s
