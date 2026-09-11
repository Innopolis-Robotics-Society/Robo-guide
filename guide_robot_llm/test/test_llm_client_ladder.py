"""`llm_client.ladder.complete_with_fallback()` -- порядок бэкендов, retry (llm_plam.md §4)."""

from __future__ import annotations

import threading

import pytest

from guide_robot_llm.llm_client.backend import Backend, BackendConfig
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.llm_client.ladder import complete_with_fallback
from guide_robot_llm.llm_client.telemetry import ClientTelemetry
from test.mocks.mock_llm_server import MockLlmServer

_MESSAGES = [{"role": "user", "content": "привет"}]
_DATA_URL = "data:image/jpeg;base64," + "QUJCDQ==" * 25
_MM_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "что на картинке?"},
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
        ],
    }
]


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


def test_frequency_penalty_threads_through_to_backend(live_server: MockLlmServer) -> None:
    backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))

    complete_with_fallback([backend], _MESSAGES, frequency_penalty=0.4)

    assert live_server.last_request_body["frequency_penalty"] == 0.4


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


# -- Taiga #3: capability-aware fallback и telemetry --------------------------


def test_fallback_from_dead_vlm_endpoint_to_compatible_live(
    live_server: MockLlmServer,
) -> None:
    """AC #3: fallback может уйти с недоступного VLM-эндпоинта на совместимый."""
    probe = MockLlmServer()  # не поднят: порт выделен ОС, connection refused
    dead_vlm = Backend(
        BackendConfig(
            base_url=probe.url,
            connect_timeout_s=0.5,
            read_timeout_s=0.5,
            multimodal_enabled=True,  # VLM-эндпоинт, но недоступен
        )
    )
    live_vlm = Backend(
        BackendConfig(base_url=live_server.url, read_timeout_s=5.0, multimodal_enabled=True)
    )
    telemetry = ClientTelemetry()

    result = complete_with_fallback(
        [dead_vlm, live_vlm],
        _MM_MESSAGES,
        max_attempts_per_backend=2,
        backoff_s=0.0,
        telemetry=telemetry,
    )

    assert result.text == "ок"
    snap = telemetry.snapshot()
    assert snap["retries"] == 1
    assert snap["fallbacks"] == 1
    assert snap["skipped_incompatible"] == 0


def test_image_request_skips_text_only_endpoint(
    live_server: MockLlmServer,
) -> None:
    """Text-only эндпоинт в цепочке пропускается для запроса с кадрами без HTTP-попыток."""
    text_only_server = MockLlmServer()
    text_only_server.chunks = ["текст"]
    text_only_server.start()
    try:
        text_only = Backend(BackendConfig(base_url=text_only_server.url, read_timeout_s=5.0))
        mm = Backend(
            BackendConfig(base_url=live_server.url, read_timeout_s=5.0, multimodal_enabled=True)
        )
        telemetry = ClientTelemetry()

        result = complete_with_fallback(
            [text_only, mm],
            _MM_MESSAGES,
            max_attempts_per_backend=1,
            backoff_s=0.0,
            telemetry=telemetry,
        )

        assert result.text == "ок"
        assert text_only_server.request_count == 0  # несовместимый -- без попыток
        assert live_server.request_count == 1
        assert telemetry.snapshot()["skipped_incompatible"] == 1
    finally:
        text_only_server.stop()


def test_text_only_request_not_skipped_on_text_only_backend(live_server: MockLlmServer) -> None:
    live_backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))
    telemetry = ClientTelemetry()

    result = complete_with_fallback(
        [live_backend], _MESSAGES, max_attempts_per_backend=1, backoff_s=0.0, telemetry=telemetry
    )

    assert result.text == "ок"
    assert live_server.request_count == 1
    assert telemetry.snapshot()["skipped_incompatible"] == 0


def test_all_incompatible_backends_raise_without_http() -> None:
    server = MockLlmServer()
    server.chunks = ["текст"]
    server.start()
    try:
        text_only = Backend(BackendConfig(base_url=server.url, read_timeout_s=5.0))
        telemetry = ClientTelemetry()

        with pytest.raises(BackendError):
            complete_with_fallback(
                [text_only],
                _MM_MESSAGES,
                max_attempts_per_backend=1,
                backoff_s=0.0,
                telemetry=telemetry,
            )

        assert server.request_count == 0
        assert telemetry.snapshot()["skipped_incompatible"] == 1
    finally:
        server.stop()


def test_retry_and_fallback_counts(dead_backend: Backend, live_server: MockLlmServer) -> None:
    live_backend = Backend(BackendConfig(base_url=live_server.url, read_timeout_s=5.0))
    telemetry = ClientTelemetry()

    complete_with_fallback(
        [dead_backend, live_backend],
        _MESSAGES,
        max_attempts_per_backend=2,
        backoff_s=0.0,
        telemetry=telemetry,
    )

    snap = telemetry.snapshot()
    assert snap["attempts"] == 3  # 2 на мёртвом + 1 на живом
    assert snap["retries"] == 1
    assert snap["fallbacks"] == 1
