"""Буфер продолжений потоковых Say-целей (Say.stream_id + SayStreamChunk).

dialog_agent шлёт первое готовое предложение ответа Say-целью, а остальные --
кусками в топик, пока LLM ещё генерирует. Куски могут прийти раньше, чем
цель дойдёт до `_speak()` (очередь планировщика, round-trip action-сервера),
поэтому буфер создаётся по первому упоминанию stream_id с любой стороны.

Модуль без зависимостей от rclpy: тестируется в CI как обычный класс.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from guide_robot_voice.lib.chunker import Clause, TextChunker

__all__ = ["ClauseFeed", "StreamPoll", "TextStreams"]


@dataclass(frozen=True)
class StreamPoll:
    """Результат ожидания: новые куски после курсора и состояние потока."""

    pieces: tuple[str, ...]
    final: bool
    cancelled: bool

    @property
    def done(self) -> bool:
        """Больше ждать нечего."""
        return self.final or self.cancelled


@dataclass
class _Stream:
    pieces: list[str] = field(default_factory=list)
    final: bool = False
    cancelled: bool = False
    touched: float = field(default_factory=time.monotonic)
    attached: bool = False


class TextStreams:
    """Потокобезопасный реестр потоков по stream_id."""

    def __init__(self, orphan_ttl_s: float = 30.0) -> None:
        """`orphan_ttl_s` -- сколько жить кускам, чья цель так и не пришла."""
        self._orphan_ttl_s = orphan_ttl_s
        self._cv = threading.Condition()
        self._streams: dict[str, _Stream] = {}

    def feed(
        self, stream_id: str, text: str, *, final: bool = False, cancel: bool = False
    ) -> None:
        """Принять кусок из топика."""
        if not stream_id:
            return
        with self._cv:
            self._purge_orphans()
            stream = self._streams.setdefault(stream_id, _Stream())
            stream.touched = time.monotonic()
            if stream.final or stream.cancelled:
                return
            if text.strip():
                stream.pieces.append(text.strip())
            stream.final = stream.final or final
            stream.cancelled = stream.cancelled or cancel
            self._cv.notify_all()

    def attach(self, stream_id: str) -> None:
        """Цель начала говорить: поток больше не сирота и не вычищается по TTL."""
        with self._cv:
            stream = self._streams.setdefault(stream_id, _Stream())
            stream.attached = True
            stream.touched = time.monotonic()

    def wait(self, stream_id: str, cursor: int, timeout: float) -> StreamPoll:
        """Дождаться куска с индексом >= cursor, final/cancel или таймаута."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                stream = self._streams.get(stream_id)
                if stream is None:
                    return StreamPoll(pieces=(), final=True, cancelled=False)
                if len(stream.pieces) > cursor or stream.final or stream.cancelled:
                    return StreamPoll(
                        pieces=tuple(stream.pieces[cursor:]),
                        final=stream.final,
                        cancelled=stream.cancelled,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return StreamPoll(pieces=(), final=False, cancelled=False)
                self._cv.wait(remaining)

    def release(self, stream_id: str) -> None:
        """Цель завершилась: поток больше не нужен."""
        with self._cv:
            self._streams.pop(stream_id, None)
            self._cv.notify_all()

    def _purge_orphans(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, stream in self._streams.items()
            if not stream.attached and now - stream.touched > self._orphan_ttl_s
        ]
        for key in stale:
            del self._streams[key]


class ClauseFeed:
    """Клаузы одной Say-цели: обычной -- сразу все, потоковой -- по мере прихода кусков.

    `text` растёт вместе с потоком: `Clause.char_end` считается от начала
    полного текста реплики, чтобы `Say.Result.spoken_text` оставался
    префиксом того, что посетитель реально услышал. Итерация кончается на
    final, cancel (`cancelled`), `should_stop()` (вытеснение/отмена цели --
    вызывающий разбирается сам) или `idle_timeout_s` без новых кусков
    (`timed_out`).
    """

    def __init__(  # noqa: PLR0913 -- всё после text keyword-only
        self,
        chunker: TextChunker,
        text: str,
        *,
        streams: TextStreams | None = None,
        stream_id: str = "",
        should_stop: Callable[[], bool] = lambda: False,
        idle_timeout_s: float = 10.0,
        poll_s: float = 0.05,
    ) -> None:
        """Без `streams`/`stream_id` -- обычная цель: клаузы одного `text`."""
        self._chunker = chunker
        self._streams = streams
        self._stream_id = stream_id if streams is not None else ""
        self._should_stop = should_stop
        self._idle_timeout_s = idle_timeout_s
        self._poll_s = poll_s
        self.text = text
        self.count = 0
        self.cancelled = False
        self.timed_out = False
        if self._stream_id and streams is not None:
            streams.attach(self._stream_id)
        self._first = self._shift(chunker.split(text), 0)

    @property
    def streaming(self) -> bool:
        """Цель потоковая."""
        return bool(self._stream_id)

    def __iter__(self) -> Iterator[Clause]:
        """Отдавать клаузы в порядке произнесения."""
        # Ход прерван, пока цель стояла в очереди -- не начинать вовсе.
        streams = self._streams
        if (
            self._stream_id
            and streams is not None
            and streams.wait(self._stream_id, 0, 0).cancelled
        ):
            self.cancelled = True
            return
        yield from self._first
        if not self._stream_id or self._streams is None:
            return
        cursor = 0
        idle_deadline = time.monotonic() + self._idle_timeout_s
        while not self._should_stop():
            poll = self._streams.wait(self._stream_id, cursor, self._poll_s)
            if poll.cancelled:
                self.cancelled = True
                return
            for piece in poll.pieces:
                cursor += 1
                separator = " " if self.text and not self.text.endswith(" ") else ""
                offset = len(self.text) + len(separator)
                self.text += separator + piece
                yield from self._shift(self._chunker.split(piece), offset)
                idle_deadline = time.monotonic() + self._idle_timeout_s
            if poll.final:
                return
            if not poll.pieces and time.monotonic() > idle_deadline:
                self.timed_out = True
                return

    def _shift(self, clauses: list[Clause], offset: int) -> list[Clause]:
        shifted = []
        for clause in clauses:
            shifted.append(
                Clause(
                    text=clause.text,
                    index=self.count,
                    char_start=clause.char_start + offset,
                    char_end=clause.char_end + offset,
                    terminal=clause.terminal,
                )
            )
            self.count += 1
        return shifted
