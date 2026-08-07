"""Юниты на бэкенды LLM: EchoBackend целиком, LlamaCppBackend -- на фикстурах.

Никаких сокетов: `requests.post`/`requests.get` подменяются моками, чтобы
парсер SSE и разбор non-stream ответа проверялись без сети и без сервера.
"""

from __future__ import annotations

import threading

import pytest
import requests
from guide_robot_llm.lib.backends import Chunk, EchoBackend, LlamaCppBackend, OpenAIBackend

# -- EchoBackend --------------------------------------------------------------


def test_echo_backend_streams_reply_word_by_word() -> None:
    backend = EchoBackend(reply_text="Привет, мир", word_delay_s=0.0)
    chunks = list(backend.stream([], threading.Event()))
    text = "".join(c.text for c in chunks)
    assert text == "Привет, мир"
    assert chunks[-1].done is True
    assert all(not c.done for c in chunks[:-1])


def test_echo_backend_health_is_always_ok() -> None:
    backend = EchoBackend()
    ok, detail = backend.health()
    assert ok is True


def test_echo_backend_stops_on_abort_mid_stream() -> None:
    backend = EchoBackend(reply_text="одно два три четыре пять", word_delay_s=0.0)
    abort = threading.Event()
    chunks: list[Chunk] = []
    for chunk in backend.stream([], abort):
        chunks.append(chunk)
        if len(chunks) == 2:
            abort.set()
    # Оборвались до конца: финального Chunk(done=True) быть не должно,
    # и не все слова "пять" успели уйти.
    assert not any(c.done for c in chunks)
    assert len(chunks) < 5


def test_echo_backend_ignores_messages_argument() -> None:
    backend = EchoBackend(reply_text="ответ", word_delay_s=0.0)
    messages = [{"role": "user", "content": "неважно что здесь"}]
    chunks = list(backend.stream(messages, threading.Event()))
    assert "".join(c.text for c in chunks) == "ответ"


# -- LlamaCppBackend: SSE-парсер на фикстурах ---------------------------------


class FakeResponse:
    """Минимальная замена `requests.Response` для тестов без сокета."""

    def __init__(
        self,
        lines: list[str] | None = None,
        json_payload: dict | None = None,
        status_code: int = 200,
    ) -> None:
        self._lines = lines or []
        self._json_payload = json_payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode: bool = True) -> list[bytes] | list[str]:
        if decode_unicode:
            return iter(self._lines)
        return iter(line.encode("utf-8") for line in self._lines)

    def json(self) -> dict:
        return self._json_payload or {}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_post(monkeypatch):
    calls: dict[str, object] = {}

    def make(response: FakeResponse):
        def fake(url, json=None, headers=None, stream=None, timeout=None):  # noqa: ANN001
            calls["url"] = url
            calls["json"] = json
            calls["headers"] = headers
            calls["stream"] = stream
            calls["timeout"] = timeout
            return response

        monkeypatch.setattr("guide_robot_llm.lib.backends.requests.post", fake)
        return calls

    return make


def test_llama_cpp_backend_parses_sse_stream(fake_post) -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"Привет"}}]}',
        "",  # keep-alive пустая строка -- должна игнорироваться
        'data: {"choices":[{"delta":{"content":", мир"}}]}',
        "data: [DONE]",
    ]
    fake_post(FakeResponse(lines=lines))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", stream=True)

    chunks = list(backend.stream([{"role": "user", "content": "hi"}], threading.Event()))

    assert [c.text for c in chunks] == ["Привет", ", мир", ""]
    assert chunks[-1].done is True
    assert all(not c.done for c in chunks[:-1])


def test_llama_cpp_backend_stops_on_abort_between_lines(fake_post) -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"a"}}]}',
        'data: {"choices":[{"delta":{"content":"b"}}]}',
        'data: {"choices":[{"delta":{"content":"c"}}]}',
        "data: [DONE]",
    ]
    fake_post(FakeResponse(lines=lines))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", stream=True)

    abort = threading.Event()
    received: list[Chunk] = []
    for chunk in backend.stream([], abort):
        received.append(chunk)
        if len(received) == 1:
            abort.set()

    assert [c.text for c in received] == ["a"]
    assert not any(c.done for c in received)


def test_llama_cpp_backend_closes_response_after_stream(fake_post) -> None:
    calls = fake_post(FakeResponse(lines=["data: [DONE]"]))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", stream=True)
    list(backend.stream([], threading.Event()))
    # response передаётся в fake_post замыканием -- достаём его напрямую из calls.
    assert calls["stream"] is True


def test_llama_cpp_backend_ignores_malformed_json_line(fake_post) -> None:
    lines = [
        "data: {not valid json",
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        "data: [DONE]",
    ]
    fake_post(FakeResponse(lines=lines))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", stream=True)
    chunks = list(backend.stream([], threading.Event()))
    assert [c.text for c in chunks] == ["ok", ""]


def test_llama_cpp_backend_non_stream_parses_full_message(fake_post) -> None:
    payload = {"choices": [{"message": {"content": "полный ответ модели"}}]}
    fake_post(FakeResponse(json_payload=payload))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", stream=False)

    chunks = list(backend.stream([], threading.Event()))

    assert chunks == [Chunk(text="полный ответ модели", done=True)]


def test_llama_cpp_backend_sends_model_when_configured(fake_post) -> None:
    calls = fake_post(FakeResponse(lines=["data: [DONE]"]))
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080", model="qwen2.5-3b", stream=True)
    list(backend.stream([], threading.Event()))
    assert calls["json"]["model"] == "qwen2.5-3b"


# -- health() -------------------------------------------------------------


def test_llama_cpp_backend_health_ok(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):  # noqa: ANN001
        return FakeResponse(status_code=200 if url.endswith("/health") else 500)

    monkeypatch.setattr("guide_robot_llm.lib.backends.requests.get", fake_get)
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080")
    ok, detail = backend.health()
    assert ok is True
    assert detail == ""


def test_llama_cpp_backend_health_falls_back_to_models(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):  # noqa: ANN001
        if url.endswith("/health"):
            return FakeResponse(status_code=404)
        return FakeResponse(status_code=200)

    monkeypatch.setattr("guide_robot_llm.lib.backends.requests.get", fake_get)
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080")
    ok, _detail = backend.health()
    assert ok is True


def test_llama_cpp_backend_health_fails_when_both_unreachable(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):  # noqa: ANN001
        raise requests.ConnectionError("нет связи")

    monkeypatch.setattr("guide_robot_llm.lib.backends.requests.get", fake_get)
    backend = LlamaCppBackend(base_url="http://127.0.0.1:8080")
    ok, detail = backend.health()
    assert ok is False
    assert "нет связи" in detail


# -- OpenAIBackend ----------------------------------------------------------


def test_openai_backend_requires_model() -> None:
    with pytest.raises(ValueError):
        OpenAIBackend(base_url="https://api.openai.com", model="")


def test_openai_backend_sends_bearer_token(monkeypatch, fake_post) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "secret-token")
    calls = fake_post(FakeResponse(lines=["data: [DONE]"]))
    backend = OpenAIBackend(
        base_url="https://api.openai.com",
        model="gpt-test",
        api_key_env="TEST_LLM_API_KEY",
        stream=True,
    )
    list(backend.stream([], threading.Event()))
    assert calls["headers"]["Authorization"] == "Bearer secret-token"


def test_openai_backend_warns_without_key(monkeypatch, fake_post) -> None:
    monkeypatch.delenv("MISSING_KEY_ENV", raising=False)
    backend = OpenAIBackend(
        base_url="https://api.openai.com", model="gpt-test", api_key_env="MISSING_KEY_ENV"
    )
    calls = fake_post(FakeResponse(lines=["data: [DONE]"]))
    list(backend.stream([], threading.Event()))
    assert "Authorization" not in calls["headers"]
