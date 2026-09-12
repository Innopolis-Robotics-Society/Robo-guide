"""`test/mocks/mock_llm_server.py` -- валидация content и fault-режимы (Taiga #3)."""

from __future__ import annotations

import threading
import time

import pytest

from guide_robot_llm.llm_client.backend import Backend, BackendConfig
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from test.mocks.mock_llm_server import MockLlmServer, validate_content

_DATA_URL = "data:image/jpeg;base64," + "QUJCDQ==" * 100  # 800 байт base64
_MULTIMODAL_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "что на картинке?"},
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
        ],
    }
]


@pytest.fixture
def mock_server():
    server = MockLlmServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _backend(server: MockLlmServer) -> Backend:
    return Backend(BackendConfig(base_url=server.url, read_timeout_s=5.0))


# -- validate_content (чистая функция) ------------------------------------


def test_validate_content_text_only_string() -> None:
    result = validate_content({"messages": [{"role": "user", "content": "привет"}]})

    assert result["ok"] is True
    assert result["kinds"] == ["text"]


def test_validate_content_multimodal_array() -> None:
    result = validate_content({"messages": _MULTIMODAL_MESSAGES})

    assert result["ok"] is True
    assert result["kinds"] == ["text", "image_url"]


def test_validate_content_non_data_url_image_rejected() -> None:
    bad_image = {"type": "image_url", "image_url": {"url": "http://x/y.jpg"}}
    messages = [{"role": "user", "content": [bad_image]}]

    result = validate_content({"messages": messages})

    assert result["ok"] is False
    assert "data-URL" in result["error"]


def test_validate_content_unknown_part_type_rejected() -> None:
    messages = [{"role": "user", "content": [{"type": "audio"}]}]

    result = validate_content({"messages": messages})

    assert result["ok"] is False


def test_validate_content_missing_messages_rejected() -> None:
    result = validate_content({"other": 1})

    assert result["ok"] is False
    assert result["kinds"] == []


# -- redacted request metadata --------------------------------------------


def test_request_meta_redacts_auth_header_and_image_payload(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["ок"]
    backend = Backend(
        BackendConfig(
            base_url=mock_server.url,
            api_key="super-secret-key",
            read_timeout_s=5.0,
            multimodal_enabled=True,  # кадры в запросе -- эндпоинт их принимает
        )
    )

    backend.complete(_MULTIMODAL_MESSAGES)

    meta = mock_server.last_request_meta
    assert meta is not None
    assert meta["headers"]["Authorization"] == "<redacted>"
    assert "super-secret-key" not in str(meta)
    assert _DATA_URL not in str(meta)
    assert "data:image/jpeg;base64,<<REDACTED 800 bytes>>" in meta["body_redacted"]
    assert meta["content_validation"]["ok"] is True
    assert meta["content_validation"]["kinds"] == ["text", "image_url"]
    assert mock_server.request_count == 1
    assert mock_server.requests_log == [meta]
    # Сырое тело для совместимости с старыми тестами сохраняется без маскирования.
    assert mock_server.last_request_body is not None
    assert _DATA_URL in str(mock_server.last_request_body)


def test_request_meta_text_only_content_kinds(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["ок"]
    backend = _backend(mock_server)

    backend.complete([{"role": "user", "content": "привет"}])

    assert mock_server.last_request_meta["content_validation"] == {
        "ok": True,
        "kinds": ["text"],
        "error": None,
    }


# -- fault-режимы (Taiga #3) ----------------------------------------------


def test_malformed_json_raises_backend_error(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_MALFORMED_JSON
    backend = _backend(mock_server)

    with pytest.raises(BackendError):
        backend.complete([{"role": "user", "content": "привет"}])


def test_disconnect_raises_backend_error(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_DISCONNECT
    backend = _backend(mock_server)

    with pytest.raises(BackendError):
        backend.complete([{"role": "user", "content": "привет"}])


def test_mid_stream_failure_raises_after_partial_tokens(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_MID_STREAM_FAILURE
    mock_server.chunks = ["раз", "два", "три", "четыре"]
    mock_server.fail_after_chunks = 2
    backend = _backend(mock_server)
    seen: list[str] = []

    with pytest.raises(BackendError):
        backend.complete([{"role": "user", "content": "привет"}], on_delta=seen.append)

    assert seen == ["раз", "два"]


def test_delayed_first_token_delays_start_but_succeeds(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_DELAYED_FIRST_TOKEN
    mock_server.first_token_delay_s = 0.5
    mock_server.chunks = ["раз", "два"]
    backend = _backend(mock_server)

    start = time.monotonic()
    result = backend.complete([{"role": "user", "content": "привет"}])
    elapsed = time.monotonic() - start

    assert result.text == "раздва"
    assert elapsed >= 0.5


def test_slow_abort_still_works_after_refactor(mock_server: MockLlmServer) -> None:
    """Регрессия: поведение MODE_SLOW/abort не изменилось после добавления режимов #3."""
    mock_server.mode = MockLlmServer.MODE_SLOW
    mock_server.chunks = ["раз", "два", "три"]
    mock_server.chunk_delay_s = 0.2
    backend = _backend(mock_server)
    abort_event = threading.Event()
    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            backend.complete([{"role": "user", "content": "привет"}], abort_event=abort_event)
        except BackendAborted as error:
            outcome["error"] = error

    thread = threading.Thread(target=_run)
    start = time.monotonic()
    thread.start()
    time.sleep(0.05)
    abort_event.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), BackendAborted)
    assert time.monotonic() - start < 1.0
