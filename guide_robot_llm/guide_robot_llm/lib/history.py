"""Кольцо диалоговых пар для истории чата.

В историю попадает `spoken_text`, а не то, что модель фактически сгенерировала
(`generated`). Если ход прервали на середине клаузы, ассистентская реплика
в истории обязана заканчиваться там же, где замолчал робот, -- иначе
следующий ход придёт к модели с контекстом, которого посетитель никогда
не слышал, и ответ перестанет быть последовательным для него.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

__all__ = ["History", "HistoryTurn"]


@dataclass(frozen=True)
class HistoryTurn:
    """Одна диалоговая пара, как она попадает в промпт."""

    user_text: str
    spoken_text: str
    interrupted: bool


class History:
    """Кольцевой буфер последних `max_history_turns` диалоговых пар."""

    def __init__(
        self,
        max_history_turns: int = 6,
        interrupted_marker: str = " [прервано]",
    ) -> None:
        """Создать историю с указанным окном и маркером прерывания."""
        self._marker = interrupted_marker
        self._turns: deque[HistoryTurn] = deque(maxlen=max(1, max_history_turns))

    def append_turn(self, user_text: str, spoken_text: str, interrupted: bool) -> None:
        """Добавить ход. Самый старый ход вытесняется при переполнении окна."""
        self._turns.append(
            HistoryTurn(user_text=user_text, spoken_text=spoken_text, interrupted=interrupted)
        )

    def window(self) -> list[dict[str, str]]:
        """Окно истории как список сообщений `{"role", "content"}`, без системного промпта."""
        messages: list[dict[str, str]] = []
        for turn in self._turns:
            messages.append({"role": "user", "content": turn.user_text})
            content = turn.spoken_text
            if turn.interrupted:
                content += self._marker
            messages.append({"role": "assistant", "content": content})
        return messages

    def __len__(self) -> int:
        """Число диалоговых пар, сейчас хранимых в окне."""
        return len(self._turns)
