"""Юниты на буфер потоковых Say-целей и сборку клауз из кусков."""

from __future__ import annotations

import threading
import time

from guide_robot_voice.lib.chunker import TextChunker
from guide_robot_voice.lib.text_stream import ClauseFeed, TextStreams


def _texts(feed: ClauseFeed) -> list[str]:
    return [clause.text for clause in feed]


def test_static_feed_is_plain_split() -> None:
    """Без stream_id -- ровно то, что даёт чанкер."""
    chunker = TextChunker()
    text = "Первое предложение про лидар. Второе предложение про сонар."
    feed = ClauseFeed(chunker, text)
    assert _texts(feed) == [clause.text for clause in chunker.split(text)]
    assert feed.text == text


def test_pieces_fed_before_goal_are_spoken_in_order() -> None:
    """Куски, пришедшие раньше цели, не теряются; char_end -- по полному тексту."""
    streams = TextStreams()
    streams.feed("s", "Второе длинное предложение ответа робота.")
    streams.feed("s", "Третье длинное предложение ответа робота.", final=True)
    feed = ClauseFeed(
        TextChunker(), "Первое длинное предложение ответа.", streams=streams, stream_id="s"
    )
    clauses = list(feed)
    assert [clause.text for clause in clauses] == [
        "Первое длинное предложение ответа.",
        "Второе длинное предложение ответа робота.",
        "Третье длинное предложение ответа робота.",
    ]
    for clause in clauses:
        assert feed.text[clause.char_start : clause.char_end].strip() == clause.text
    assert [clause.index for clause in clauses] == [0, 1, 2]
    assert not feed.cancelled
    assert not feed.timed_out


def test_pieces_arriving_while_speaking_are_awaited() -> None:
    """Итерация ждёт следующего куска и кончается на final."""
    streams = TextStreams()
    feed = ClauseFeed(TextChunker(), "Начало ответа робота.", streams=streams, stream_id="s")

    def producer() -> None:
        time.sleep(0.05)
        streams.feed("s", "Продолжение ответа робота.")
        time.sleep(0.05)
        streams.feed("s", "", final=True)

    thread = threading.Thread(target=producer)
    thread.start()
    assert _texts(feed) == ["Начало ответа робота.", "Продолжение ответа робота."]
    thread.join()


def test_cancel_stops_without_speaking_rest() -> None:
    """cancel -- остаток не звучит, флаг выставлен."""
    streams = TextStreams()
    feed = ClauseFeed(TextChunker(), "Начало ответа робота.", streams=streams, stream_id="s")
    iterator = iter(feed)
    assert next(iterator).text == "Начало ответа робота."
    streams.feed("s", "Не должно прозвучать.", cancel=True)
    assert list(iterator) == []
    assert feed.cancelled


def test_cancel_before_goal_starts_speaks_nothing() -> None:
    """Ход прерван, пока цель в очереди -- даже первое предложение не звучит."""
    streams = TextStreams()
    streams.feed("s", "", cancel=True)
    feed = ClauseFeed(TextChunker(), "Начало ответа робота.", streams=streams, stream_id="s")
    assert _texts(feed) == []
    assert feed.cancelled


def test_idle_timeout_ends_stream() -> None:
    """Нет ни кусков, ни final -- реплика закрывается по таймауту."""
    streams = TextStreams()
    streams.attach("s")
    feed = ClauseFeed(
        TextChunker(),
        "Начало ответа робота.",
        streams=streams,
        stream_id="s",
        idle_timeout_s=0.1,
        poll_s=0.02,
    )
    assert _texts(feed) == ["Начало ответа робота."]
    assert feed.timed_out


def test_should_stop_breaks_wait() -> None:
    """Вытеснение цели прерывает ожидание кусков."""
    streams = TextStreams()
    stop = threading.Event()
    feed = ClauseFeed(
        TextChunker(),
        "Начало ответа робота.",
        streams=streams,
        stream_id="s",
        should_stop=stop.is_set,
        poll_s=0.02,
    )
    iterator = iter(feed)
    next(iterator)
    stop.set()
    assert list(iterator) == []
    assert not feed.timed_out


def test_orphan_streams_are_purged() -> None:
    """Куски без цели не копятся вечно."""
    streams = TextStreams(orphan_ttl_s=0.0)
    streams.feed("old", "Кусок без цели.")
    time.sleep(0.01)
    streams.feed("new", "Другой кусок.")
    assert streams.wait("old", 0, 0.0).final  # поток вычищен -- ждать нечего
