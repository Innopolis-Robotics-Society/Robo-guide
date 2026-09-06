"""`llm_client.ladder.complete_with_fallback()` -- порядок бэкендов, retry (llm_plam.md §4)."""

from __future__ import annotations

import threading

import pytest
from guide_robot_llm.llm_client.backend import Backend, BackendConfig
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.llm_client.ladder import complete_with_fallback

from test.mocks.mock_llm_server import MockLlmServer

_MESSAGES = [{"role": "user", "content": "привет"}]


@pytest.fixture
def dead_backend() -> Backend:
    """Порт выделен ОС, но сервер не запущен -- гарантированный connection refused."""
    probe = MockLlmServer()
    return Backend(BackendConfig(base_url=probe.url, connect_timeout_s=0.5, read_timeout_s=0.5))


@pytest.fixture
def live_server():
    server = MockLlmServer()
    server.chunks = ["ок"]
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_first_backend_unavailable_falls_back_to_second(
    dead_backend: Backend, live_server: MockLlmServer
) -> None:
    live_backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))

    result = complete_with_fallback(
        [dead_backend, live_backend], _MESSAGES, max_attempts_per_backend=1, backoff_s=0.0
    )

    assert result.text == "ок"


def test_all_backends_unavailable_raises(dead_backend: Backend) -> None:
    other_dead = MockLlmServer()
    other_dead_backend = Backend(
        BackendConfig(base_url=other_dead.url, connect_timeout_s=0.5, read_timeout_s=0.5)
    )

    with pytest.raises(BackendError):
        complete_with_fallback(
            [dead_backend, other_dead_backend],
            _MESSAGES,
            max_attempts_per_backend=1,
            backoff_s=0.0,
        )


def test_aborted_on_first_backend_does_not_retry_on_second(live_server: MockLlmServer) -> None:
    live_server.mode = MockLlmServer.MODE_SLOW
    live_server.chunks = ["раз", "два", "три"]
    live_server.chunk_delay_s = 0.05
    backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))
    abort_event = threading.Event()
    abort_event.set()  # уже взведён -- прервётся на первом же чанке

    never_called = MockLlmServer()  # не поднят -- если до него дойдут, тест это заметит

    with pytest.raises(BackendAborted):
        complete_with_fallback(
            [backend, Backend(BackendConfig(base_url=never_called.url, connect_timeout_s=0.5))],
            _MESSAGES,
            abort_event=abort_event,
            max_attempts_per_backend=2,
            backoff_s=0.0,
        )
