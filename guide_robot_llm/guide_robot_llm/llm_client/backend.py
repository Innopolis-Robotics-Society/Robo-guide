"""Один HTTP-бэкенд поверх OpenAI-совместимого `/v1/chat/completions` (llm_plam.md §4).

Контракт сервера -- `llm_server/iros_llm_server_SPEC.md` §0/§6: стриминг SSE,
GBNF передаётся per-request в теле (не файлом на сервере), раздельные
connect/read таймауты (сеть локальная -- коннект быстрый, генерация идёт
секундами). Этот модуль -- только транспорт: как собрать `messages` (system
prompt, история, снапшот, порядок статика-перед-волатильным для
`CACHE_REUSE`) -- дело вызывающего (`dialog_agent`, шаг 5), здесь `messages`
просто пересылаются как дали.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

import requests

from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)

__all__ = ["Backend", "BackendConfig", "CompletionResult"]

_DONE = "[DONE]"


@dataclass(frozen=True)
class BackendConfig:
    """Один бэкенд: адрес + раздельные таймауты."""

    base_url: str  # "http://host:port/v1", без хвостового "/"
    api_key: str = ""  # пусто -- заголовок Authorization не шлём
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 30.0


@dataclass
class CompletionResult:
    """Итог одного успешного вызова -- собранный текст + причина остановки от сервера."""

    text: str
    finish_reason: str = ""


class Backend:
    """Один `base_url`. Синхронный вызов, всегда стримит внутри -- см. `complete()`."""

    def __init__(self, config: BackendConfig, *, session: requests.Session | None = None) -> None:
        """Запомнить конфиг; `session` подменяется в тестах (мок-сервер на localhost)."""
        self._config = config
        self._session = session or requests.Session()

    def complete(
        self,
        messages: list[dict],
        *,
        grammar: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        abort_event: threading.Event | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        """POST `.../chat/completions` со `stream=true`, разобрать SSE, собрать полный текст.

        Стрим -- не опция, а необходимость: `requests` не даёт прервать уже
        начатый блокирующий (нестримящий) вызов из другого потока, а abort по
        barge-in (llm_plam.md §6: "abort HTTP-запроса, не просто игнор
        ответа") обязан реально закрывать соединение, не имитацией. Между
        чанками -- единственная точка, где можно проверить `abort_event` и
        оборвать генерацию на сервере, не дожидаясь остатка.

        `read_timeout_s` в `requests` -- таймаут между чтениями сокета, не на
        весь ответ целиком: пока сервер шлёт дельты с паузами короче
        `read_timeout_s`, многосекундная генерация не заденет его.
        """
        payload: dict[str, object] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if grammar:
            payload["grammar"] = grammar

        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        timeout = (self._config.connect_timeout_s, self._config.read_timeout_s)

        try:
            response = self._session.post(
                url, json=payload, headers=headers, timeout=timeout, stream=True
            )
        except requests.exceptions.Timeout as error:
            raise BackendTimeout(str(error)) from error
        except requests.exceptions.RequestException as error:
            raise BackendError(str(error)) from error

        if response.status_code != 200:
            body = response.text
            response.close()
            raise BackendHTTPError(response.status_code, body)

        return self._consume_stream(response, abort_event=abort_event, on_delta=on_delta)

    def _consume_stream(
        self,
        response: requests.Response,
        *,
        abort_event: threading.Event | None,
        on_delta: Callable[[str], None] | None,
    ) -> CompletionResult:
        chunks: list[str] = []
        finish_reason = ""
        try:
            for raw_bytes in response.iter_lines():
                # НЕ decode_unicode=True: requests угадывает кодировку по
                # Content-Type, а llama.cpp не шлёт `charset=utf-8` для
                # text/event-stream -- requests молча откатывается на
                # ISO-8859-1 (старый HTTP-дефолт для text/*), и кириллица
                # превращается в мусор ("Ð..."), не в ошибку -- баг
                # воспроизведён вживую на реальном llm_server. JSON, а
                # значит и SSE-payload здесь, по конвенции UTF-8 всегда --
                # декодируем сами, не полагаясь на угадывание requests.
                if abort_event is not None and abort_event.is_set():
                    msg = "abort_event взведён во время стрима"
                    raise BackendAborted(msg)
                if not raw_bytes:
                    continue
                try:
                    raw_line = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    msg = f"не-UTF-8 байты в SSE: {raw_bytes[:200]!r}"
                    raise BackendError(msg) from error
                if not raw_line.startswith("data:"):
                    continue
                data = raw_line[len("data:") :].strip()
                if data == _DONE:
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as error:
                    msg = f"битый JSON в SSE: {data[:200]!r}"
                    raise BackendError(msg) from error
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = (choice.get("delta") or {}).get("content") or ""
                if delta:
                    chunks.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                reason = choice.get("finish_reason")
                if reason:
                    finish_reason = reason
        except requests.exceptions.Timeout as error:
            raise BackendTimeout(str(error)) from error
        except requests.exceptions.RequestException as error:
            raise BackendError(str(error)) from error
        finally:
            response.close()
        return CompletionResult(text="".join(chunks), finish_reason=finish_reason)
