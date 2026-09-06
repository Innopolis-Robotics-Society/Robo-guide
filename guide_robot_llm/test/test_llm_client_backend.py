"""`llm_client.backend.Backend` -- HTTP-механика поверх мок-сервера (llm_plam.md §4)."""

from __future__ import annotations

import threading
import time

import pytest
from guide_robot_llm.llm_client.backend import Backend, BackendConfig
from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)

from test.mocks.mock_llm_server import MockLlmServer

_MESSAGES = [{"role": "user", "content": "привет"}]


@pytest.fixture
def mock_server():
    server = MockLlmServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_complete_collects_full_text_from_chunks(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["Привет", ", ", "мир", "."]
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    result = backend.complete(_MESSAGES)

    assert result.text == "Привет, мир."
    assert result.finish_reason == "stop"


def test_cyrillic_survives_without_charset_in_content_type(mock_server: MockLlmServer) -> None:
    """Регрессия: llama.cpp не шлёт `charset=utf-8` в `Content-Type: text/event-stream`
    (как и мок здесь -- см. mock_llm_server.py), `requests` в таком случае молча
    откатывается на кодировку по умолчанию для `text/*`, не на UTF-8, и кириллица
    превращается в "Ð..." -- воспроизведено на реальном llm_server. Один длинный
    реалистичный ход, не короткое "привет" -- чтобы не зависеть от эвристики
    угадывания кодировки, которая на коротких строках может случайно угадать верно.
    """
    greeting = (
        "Здравствуйте! Я рад приветствовать вас в нашем музее. "
        "Меня зовут Дмитрий, я буду вашим гидом сегодня. "
        "Готовы начать увлекательную экскурсию по залам?"
    )
    mock_server.chunks = [greeting[i : i + 7] for i in range(0, len(greeting), 7)]
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    result = backend.complete(_MESSAGES)

    assert result.text == greeting


def test_on_delta_called_for_each_nonempty_chunk(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["A", "B", "C"]
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))
    seen: list[str] = []

    backend.complete(_MESSAGES, on_delta=seen.append)

    assert seen == ["A", "B", "C"]


def test_read_timeout_raises_backend_timeout(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_HANG
    mock_server.hang_s = 5.0
    backend = Backend(
        BackendConfig(base_url=mock_server.url, connect_timeout_s=1.0, read_timeout_s=0.2)
    )

    with pytest.raises(BackendTimeout):
        backend.complete(_MESSAGES)


def test_connection_refused_raises_backend_error() -> None:
    server = MockLlmServer()
    dead_url = server.url  # порт выделен ОС, но сервер не запущен -- гарантированный refuse
    backend = Backend(BackendConfig(base_url=dead_url, connect_timeout_s=1.0, read_timeout_s=1.0))

    with pytest.raises(BackendError):
        backend.complete(_MESSAGES)


def test_http_error_raises_backend_http_error_with_status(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_HTTP_ERROR
    mock_server.http_status = 500
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    with pytest.raises(BackendHTTPError) as excinfo:
        backend.complete(_MESSAGES)
    assert excinfo.value.status_code == 500


def test_abort_event_stops_slow_stream_quickly(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_SLOW
    mock_server.chunks = ["раз", "два", "три", "четыре", "пять"]
    mock_server.chunk_delay_s = 0.3
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))
    abort_event = threading.Event()
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            backend.complete(_MESSAGES, abort_event=abort_event)
        except BackendAborted as error:
            result["error"] = error

    thread = threading.Thread(target=_run)
    start = time.monotonic()
    thread.start()
    time.sleep(0.1)  # дать прийти первому чанку
    abort_event.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive(), "поток не завершился -- abort не сработал"
    assert isinstance(result.get("error"), BackendAborted)
    assert elapsed < 1.0, f"abort занял {elapsed:.2f}с -- дольше одного chunk_delay_s"
