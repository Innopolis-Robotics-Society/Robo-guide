"""Приоритетная очередь высказываний с вытеснением.

По сути twist_mux для голоса, и по тем же причинам. Без приоритетов
аварийное "отойдите, робот поворачивается" встанет в очередь за
трёхминутной справкой об экспонате, а посетительский вопрос не сможет
прервать монолог.

Правила разрешения конфликтов:

  * приоритет строго выше текущего и текущее interruptible -> вытеснение;
  * иначе -> в очередь, порядок FIFO внутри одного приоритета;
  * вытесненное НЕ возвращается в очередь.

Последний пункт -- сознательное решение. Возобновлять прерванный монолог
или нет, знает narration_server, а не tts_node: посетитель мог задать
вопрос, который делает остаток справки бессмысленным. Поэтому вытесненная
цель завершается со статусом PREEMPTED и полем spoken_chars, а решение
о возобновлении принимается уровнем выше.

Модуль без зависимостей от rclpy: тестируется в CI как обычный класс.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["Action", "Decision", "Scheduler", "Scope", "Utterance"]


class Scope(int, Enum):
    """Область действия отмены. Должна совпадать с константами CancelAll.msg."""

    ALL = 0
    NARRATION = 1
    DIALOG = 2
    SAFETY = 3

    def matches(self, cancel_scope: Scope | int) -> bool:
        """Попадает ли данное высказывание под отмену с указанным scope."""
        return int(cancel_scope) == Scope.ALL or int(cancel_scope) == int(self)


@dataclass(frozen=True)
class Utterance:
    """Заявка на произнесение."""

    goal_id: str
    text: str
    priority: int
    scope: Scope = Scope.DIALOG
    voice: str = ""
    interruptible: bool = True
    max_duration: float = 0.0
    seq: int = 0

    @property
    def sort_key(self) -> tuple[int, int]:
        """Ключ упорядочивания: выше приоритет -- раньше, при равенстве FIFO."""
        return (-self.priority, self.seq)


class Action(Enum):
    """Что планировщик предписывает сделать с заявкой."""

    START = "start"
    QUEUE = "queue"
    PREEMPT = "preempt"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    """Результат submit()."""

    action: Action
    victim: Utterance | None = None
    reason: str = ""


@dataclass
class Scheduler:
    """Планировщик. Не потокобезопасен -- вызывающий обязан сериализовать."""

    max_queue: int = 8
    _active: Utterance | None = None
    _queue: list[Utterance] = field(default_factory=list)
    _counter: itertools.count = field(default_factory=itertools.count)

    # -- состояние ----------------------------------------------------------

    @property
    def active(self) -> Utterance | None:
        """Текущее произносимое высказывание."""
        return self._active

    @property
    def queued(self) -> tuple[Utterance, ...]:
        """Очередь в порядке будущего исполнения."""
        return tuple(sorted(self._queue, key=lambda u: u.sort_key))

    def next_seq(self) -> int:
        """Выдать следующий монотонный номер заявки."""
        return next(self._counter)

    # -- приём заявок -------------------------------------------------------

    def submit(self, utterance: Utterance) -> Decision:
        """Принять заявку и решить её судьбу."""
        if len(self._queue) >= self.max_queue:
            return Decision(Action.REJECT, reason="queue_full")

        if self._active is None:
            self._active = utterance
            return Decision(Action.START)

        if utterance.priority > self._active.priority and self._active.interruptible:
            victim = self._active
            self._active = utterance
            return Decision(Action.PREEMPT, victim=victim, reason="higher_priority")

        self._queue.append(utterance)
        return Decision(Action.QUEUE)

    # -- завершение ---------------------------------------------------------

    def finish(self, goal_id: str) -> Utterance | None:
        """Отметить цель завершённой и вернуть следующую к исполнению."""
        if self._active is not None and self._active.goal_id == goal_id:
            self._active = None
        else:
            self._queue = [u for u in self._queue if u.goal_id != goal_id]
            return None
        return self._promote()

    def _promote(self) -> Utterance | None:
        if not self._queue:
            return None
        self._queue.sort(key=lambda u: u.sort_key)
        self._active = self._queue.pop(0)
        return self._active

    # -- отмена -------------------------------------------------------------

    def cancel(self, scope: Scope | int = Scope.ALL) -> tuple[Utterance | None, list[Utterance]]:
        """Снять всё, что попадает под scope.

        Возвращает (вытесненное активное или None, снятые из очереди).
        Высказывания с interruptible=False переживают отмену, если scope
        не SAFETY: аварийное предупреждение не должно гаситься barge-in'ом
        от посетителя.
        """
        dropped_active: Utterance | None = None
        if self._active is not None and self._active.scope.matches(scope):
            hard = int(scope) == Scope.SAFETY
            if self._active.interruptible or hard:
                dropped_active = self._active
                self._active = None

        survivors: list[Utterance] = []
        dropped_queue: list[Utterance] = []
        for utterance in self._queue:
            if utterance.scope.matches(scope):
                dropped_queue.append(utterance)
            else:
                survivors.append(utterance)
        self._queue = survivors

        if self._active is None:
            self._promote()

        return dropped_active, dropped_queue

    def cancel_goal(self, goal_id: str) -> Utterance | None:
        """Снять одну цель по идентификатору."""
        if self._active is not None and self._active.goal_id == goal_id:
            victim = self._active
            self._active = None
            self._promote()
            return victim
        for index, utterance in enumerate(self._queue):
            if utterance.goal_id == goal_id:
                return self._queue.pop(index)
        return None
