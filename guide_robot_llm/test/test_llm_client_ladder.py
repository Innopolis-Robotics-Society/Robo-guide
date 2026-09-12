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
    return Backend(
        BackendConfig(
            base_url=probe.url, connect_timeout_s=0.5, read_timeout_s=0.5, max_attempts=1
        )
    )


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

    result = complete_with_fallback([dead_backend, live_backend], _MESSAGES, backoff_s=0.0)

    assert result.text == "ок"


def test_frequency_penalty_threads_through_to_backend(live_server: MockLlmServer) -> None:
    backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))

    complete_with_fallback([backend], _MESSAGES, frequency_penalty=0.4)

    assert live_server.last_request_body["frequency_penalty"] == 0.4


def test_all_backends_unavailable_raises(dead_backend: Backend) -> None:
    other_dead = MockLlmServer()
    other_dead_backend = Backend(
        BackendConfig(
            base_url=other_dead.url, connect_timeout_s=0.5, read_timeout_s=0.5, max_attempts=1
        )
    )

    with pytest.raises(BackendError):
        complete_with_fallback([dead_backend, other_dead_backend], _MESSAGES, backoff_s=0.0)


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
            backoff_s=0.0,
        )


# -- max_attempts (TASK_external_llm_backend.md §2): свойство бэкенда, не аргумент лестницы --


def test_max_attempts_is_per_backend_not_global() -> None:
    """Первый бэкенд с `max_attempts=3` выбирает все три попытки, второй -- ровно одну."""
    flaky = MockLlmServer()
    flaky.start()
    flaky.mode = MockLlmServer.MODE_HTTP_ERROR
    flaky.http_status = 500
    flaky.http_error_count = -1  # всегда падает -- второй бэкенд обязан ответить

    live = MockLlmServer()
    live.start()
    live.chunks = ["ок"]
    try:
        flaky_backend = Backend(
            BackendConfig(base_url=flaky.url, read_timeout_s=5.0, max_attempts=3)
        )
        live_backend = Backend(
            BackendConfig(base_url=live.url, read_timeout_s=5.0, max_attempts=1)
        )

        result = complete_with_fallback([flaky_backend, live_backend], _MESSAGES, backoff_s=0.0)

        assert result.text == "ок"
        assert flaky.request_count == 3
        assert live.request_count == 1
    finally:
        flaky.stop()
        live.stop()


# -- 4xx не ретраится, кроме 429 (TASK_external_llm_backend.md §2) --


def test_http_401_does_not_retry_moves_to_next_backend() -> None:
    unauthorized = MockLlmServer()
    unauthorized.start()
    unauthorized.mode = MockLlmServer.MODE_HTTP_ERROR
    unauthorized.http_status = 401
    unauthorized.http_error_count = -1

    live = MockLlmServer()
    live.start()
    live.chunks = ["ок"]
    try:
        unauthorized_backend = Backend(
            BackendConfig(base_url=unauthorized.url, read_timeout_s=5.0, max_attempts=3)
        )
        live_backend = Backend(BackendConfig(base_url=live.url, read_timeout_s=5.0))

        result = complete_with_fallback(
            [unauthorized_backend, live_backend], _MESSAGES, backoff_s=0.0
        )

        assert result.text == "ок"
        # Несмотря на max_attempts=3 -- 401 не ретраится, ровно один запрос.
        assert unauthorized.request_count == 1
    finally:
        unauthorized.stop()
        live.stop()


def test_http_429_retries_same_backend() -> None:
    rate_limited = MockLlmServer()
    rate_limited.start()
    rate_limited.mode = MockLlmServer.MODE_HTTP_ERROR
    rate_limited.http_status = 429
    rate_limited.http_error_count = 1  # падает один раз, второй запрос -- MODE_OK
    rate_limited.chunks = ["ок"]
    try:
        backend = Backend(
            BackendConfig(base_url=rate_limited.url, read_timeout_s=5.0, max_attempts=2)
        )

        result = complete_with_fallback([backend], _MESSAGES, backoff_s=0.0)

        assert result.text == "ок"
        assert rate_limited.request_count == 2
    finally:
        rate_limited.stop()
