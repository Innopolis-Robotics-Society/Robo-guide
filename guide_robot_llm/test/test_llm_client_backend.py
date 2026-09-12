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


# -- frequency_penalty (stage5 п.3): только когда вызывающий его просит --


def test_frequency_penalty_reaches_request_body_when_given(mock_server: MockLlmServer) -> None:
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    backend.complete(_MESSAGES, frequency_penalty=0.4)

    assert mock_server.last_request_body["frequency_penalty"] == 0.4


def test_frequency_penalty_omitted_from_request_body_by_default(
    mock_server: MockLlmServer,
) -> None:
    """Фаза действия не передаёт `frequency_penalty` -- ключ не должен идти в payload вовсе."""
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    backend.complete(_MESSAGES)

    assert "frequency_penalty" not in mock_server.last_request_body


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


def test_stop_when_ends_stream_without_abort(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ['{"tool":"', "reply", '","args":{}}', "SHOULD_NOT"]
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    result = backend.complete(
        _MESSAGES,
        stop_when=lambda text: '"tool":"reply"' in text,
    )

    assert "SHOULD_NOT" not in result.text
    assert '"tool":"reply"' in result.text
    assert result.finish_reason == "stop_when"


# -- внешний OpenAI-совместимый шлюз (TASK_external_llm_backend.md §1) -----------


def test_model_included_in_request_body_when_configured(mock_server: MockLlmServer) -> None:
    backend = Backend(
        BackendConfig(
            base_url=mock_server.url, read_timeout_s=5.0, model="deepseek/deepseek-v4.1-flash"
        )
    )

    backend.complete(_MESSAGES)

    assert mock_server.last_request_body["model"] == "deepseek/deepseek-v4.1-flash"


def test_model_omitted_from_request_body_when_empty(mock_server: MockLlmServer) -> None:
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    backend.complete(_MESSAGES)

    assert "model" not in mock_server.last_request_body


def test_structured_gbnf_sends_grammar_key(mock_server: MockLlmServer) -> None:
    backend = Backend(
        BackendConfig(base_url=mock_server.url, read_timeout_s=5.0, structured="gbnf")
    )

    backend.complete(_MESSAGES, grammar="root ::= object")

    assert mock_server.last_request_body["grammar"] == "root ::= object"
    assert "response_format" not in mock_server.last_request_body


def test_structured_json_object_sends_response_format_not_grammar(
    mock_server: MockLlmServer,
) -> None:
    backend = Backend(
        BackendConfig(base_url=mock_server.url, read_timeout_s=5.0, structured="json_object")
    )

    backend.complete(_MESSAGES, grammar="root ::= object")

    assert mock_server.last_request_body["response_format"] == {"type": "json_object"}
    assert "grammar" not in mock_server.last_request_body


def test_structured_none_sends_neither_grammar_nor_response_format(
    mock_server: MockLlmServer,
) -> None:
    backend = Backend(
        BackendConfig(base_url=mock_server.url, read_timeout_s=5.0, structured="none")
    )

    backend.complete(_MESSAGES, grammar="root ::= object")

    assert "grammar" not in mock_server.last_request_body
    assert "response_format" not in mock_server.last_request_body


def test_extra_body_reaches_payload_without_clobbering_core_keys(
    mock_server: MockLlmServer,
) -> None:
    backend = Backend(
        BackendConfig(
            base_url=mock_server.url,
            read_timeout_s=5.0,
            extra_body={
                "stream_options": {"include_usage": True},
                # Совпадающие с core-ключами имена -- core обязан победить.
                "messages": "SHOULD_NOT_SURVIVE",
                "stream": False,
            },
        )
    )

    backend.complete(_MESSAGES, max_tokens=42)

    body = mock_server.last_request_body
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"] == _MESSAGES
    assert body["stream"] is True


def test_reasoning_deltas_excluded_from_text_and_counted(mock_server: MockLlmServer) -> None:
    mock_server.reasoning_chunks = ["дума", "ю..."]
    mock_server.chunks = ["Готово."]
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    result = backend.complete(_MESSAGES)

    assert result.text == "Готово."
    assert result.reasoning_chars == len("дума") + len("ю...")


def test_only_reasoning_past_first_content_timeout_raises_backend_timeout(
    mock_server: MockLlmServer,
) -> None:
    mock_server.mode = MockLlmServer.MODE_SLOW
    mock_server.chunk_delay_s = 0.1
    mock_server.reasoning_chunks = ["думаю"] * 5
    mock_server.chunks = []  # content не приходит вовсе
    backend = Backend(
        BackendConfig(base_url=mock_server.url, read_timeout_s=5.0, first_content_timeout_s=0.2)
    )

    with pytest.raises(BackendTimeout):
        backend.complete(_MESSAGES)


def test_max_tokens_in_body_includes_reasoning_budget(mock_server: MockLlmServer) -> None:
    backend = Backend(
        BackendConfig(base_url=mock_server.url, read_timeout_s=5.0, reasoning_budget_tokens=1024)
    )

    backend.complete(_MESSAGES, max_tokens=160)

    assert mock_server.last_request_body["max_tokens"] == 160 + 1024


def test_sse_error_event_raises_backend_error(mock_server: MockLlmServer) -> None:
    mock_server.send_error = {"message": "мок ошибка шлюза", "type": "invalid_request"}
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    with pytest.raises(BackendError):
        backend.complete(_MESSAGES)


def test_gateway_warnings_and_usage_reach_completion_result(mock_server: MockLlmServer) -> None:
    mock_server.gateway_warnings = ["ignored unknown parameter 'grammar'"]
    mock_server.usage_payload = {
        "prompt_tokens": 2000,
        "prompt_tokens_details": {"cached_tokens": 512},
    }
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=5.0))

    result = backend.complete(_MESSAGES)

    assert result.gateway_warnings == ["ignored unknown parameter 'grammar'"]
    assert result.usage["prompt_tokens"] == 2000
    assert result.usage["prompt_tokens_details"]["cached_tokens"] == 512
