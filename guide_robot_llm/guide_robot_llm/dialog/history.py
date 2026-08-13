"""Память диалога между ходами -- append-only, режется по символам при записи.

Чистая логика без rclpy, тот же принцип пакета, что `matching.py`/`snapshot.py`:
`dialog_agent_node.py` -- единственный потребитель из rclpy-контекста
(DIALOG_REWORK_PLAN.md §3.1).

Обрезка текста каждой записи -- в момент `add_*`, не в `to_messages()`:
верхняя граница размера истории тогда статическая и проверяется юнит-тестом,
а не мониторится в рантайме. Осознанный отказ от подсчёта токенов --
токенизатора на клиенте нет, а `/tokenize` -- лишний round-trip на каждый ход.

В лимите `max_entries` считаются ТОЛЬКО реплики (visitor/robot) -- события
идут «прицепом»: верхняя граница размера тогда `max_entries * cap_реплики +
события между оставленными репликами * cap_event` (события порождаются только
изменением состояния FSM/действием хода -- их поток ограничен темпом ходов).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DialogHistory", "HistoryEntry"]

_EVENT_PREFIX = "СОБЫТИЕ: "
_TRUNCATED_SUFFIX = " …(реплика была прервана)"


def _render_events(events: list[str]) -> str:
    """Отрендерить блок событий -- КАЖДАЯ строка с префиксом `СОБЫТИЕ: `.

    Преамбула системного промпта описывает события как «строки, начинающиеся
    с "СОБЫТИЕ:"» -- продолжение блока без префикса модель вправе принять за
    слова посетителя.
    """
    return "\n".join(_EVENT_PREFIX + event for event in events)


@dataclass(frozen=True)
class HistoryEntry:
    """Одна запись истории. `truncated` имеет смысл только для kind="robot"."""

    kind: str  # "visitor" | "robot" | "event"
    text: str
    ts: float
    truncated: bool = False


def _truncate(text: str, cap: int) -> str:
    """Обрезать `text` до `cap` символов, по границе слова, с многоточием."""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


class DialogHistory:
    """Append-only лог реплик/событий хода диалога, ограниченный по числу и длине записей."""

    def __init__(
        self,
        *,
        max_entries: int = 16,
        trim_to: int = 8,
        cap_visitor: int = 200,
        cap_robot: int = 300,
        cap_event: int = 120,
    ) -> None:
        """Запомнить пределы; сама история пуста."""
        self._max_entries = max_entries
        self._trim_to = trim_to
        self._cap_visitor = cap_visitor
        self._cap_robot = cap_robot
        self._cap_event = cap_event
        self._entries: list[HistoryEntry] = []

    def add_visitor(self, text: str, *, ts: float) -> None:
        """Добавить реплику посетителя."""
        self._append(HistoryEntry(kind="visitor", text=_truncate(text, self._cap_visitor), ts=ts))

    def add_robot(self, text: str, *, ts: float, truncated: bool = False) -> None:
        """Добавить реплику робота. `truncated` -- озвучка была оборвана barge-in'ом."""
        self._append(
            HistoryEntry(
                kind="robot", text=_truncate(text, self._cap_robot), ts=ts, truncated=truncated
            )
        )

    def add_event(self, text: str, *, ts: float) -> None:
        """Добавить событие мимо диалога (переход FSM, исполненное действие хода и т.п.)."""
        self._append(HistoryEntry(kind="event", text=_truncate(text, self._cap_event), ts=ts))

    def _append(self, entry: HistoryEntry) -> None:
        self._entries.append(entry)
        # В лимите считаются только реплики (visitor/robot) -- события едут
        # «прицепом» при обрезке: раньше события съедали лимит, и реальная
        # память диалога сжималась до ~4-5 ходов при max_entries=16.
        reply_count = sum(1 for e in self._entries if e.kind != "event")
        if reply_count <= self._max_entries:
            return
        # Половинами, не по одному -- см. докстринг модуля/DIALOG_REWORK_PLAN.md §1:
        # оставить хвост, содержащий ровно `trim_to` реплик (события между ними
        # сохраняются, события перед первой оставленной репликой уходят).
        kept = 0
        start = len(self._entries)
        for index in range(len(self._entries) - 1, -1, -1):
            if self._entries[index].kind != "event":
                kept += 1
                if kept == self._trim_to:
                    start = index
                    break
        self._entries = self._entries[start:]

    def render(self) -> tuple[list[dict], list[str]]:
        """Отрендерить историю в `messages` + хвостовые события отдельно. Не мутирует состояние.

        Хвостовой пробег событий (после последней реплики) НЕ сбрасывается в
        отдельное user-сообщение, а возвращается списком: вызывающий код
        (`dialog_agent_node._run_turn`) вклеивает его в ТО ЖЕ сообщение, что
        и текущая реплика посетителя -- два подряд user-сообщения ломали
        8B-модель (отвечала на СОБЫТИЕ вместо реплики). Сами события при
        этом остаются в `_entries`: на следующем ходу они уже не хвостовые
        (после них легли реплики этого хода) и рендерятся обычным
        `СОБЫТИЕ:`-сообщением -- байтово стабильно для кэша префикса.
        """
        messages: list[dict] = []
        pending_events: list[str] = []

        def _flush_events() -> None:
            if pending_events:
                messages.append({"role": "user", "content": _render_events(pending_events)})
                pending_events.clear()

        for entry in self._entries:
            if entry.kind == "event":
                pending_events.append(entry.text)
                continue
            _flush_events()
            if entry.kind == "visitor":
                messages.append({"role": "user", "content": entry.text})
            else:  # "robot"
                content = entry.text + _TRUNCATED_SUFFIX if entry.truncated else entry.text
                messages.append({"role": "assistant", "content": content})
        return messages, pending_events

    def to_messages(self) -> list[dict]:
        """Как `render()`, но хвостовые события сброшены в последнее user-сообщение."""
        messages, trailing_events = self.render()
        if trailing_events:
            messages.append({"role": "user", "content": _render_events(trailing_events)})
        return messages

    def clear(self) -> None:
        """Стереть всю историю. Идемпотентно."""
        self._entries = []

    def __len__(self) -> int:
        """Число хранимых записей (после обрезки)."""
        return len(self._entries)
