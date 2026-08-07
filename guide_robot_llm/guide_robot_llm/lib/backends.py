"""Бэкенды LLM за общим интерфейсом.

Три реализации:

* `LlamaCppBackend` -- плоский HTTP к OpenAI-совместимому `/v1/chat/completions`
  (llama.cpp `llama-server`, но подходит любой сервер с тем же контрактом).
  `llama_ros` намеренно не используется: HTTP одинаково работает с локальной
  llama.cpp, машиной в LAN и внешним API, поэтому цепочка деградации
  "внешняя -> локальная -> канонные фразы" становится вопросом конфига,
  а не кода.
* `OpenAIBackend` -- тот же протокол, отличается заголовком авторизации
  (ключ из переменной окружения, не из yaml -- секреты в параметрах ROS
  утекают в `/parameter_events` и в логи `ros2 param dump`) и обязательным
  `model`.
* `EchoBackend` -- детерминированный ответ без сети, аналог `NullBackend`
  из `guide_robot_voice`: нужен для CI и для проверки петли на Stage 0
  без поднятого сервера модели.

Оба HTTP-бэкенда поддерживают потоковый (`stream=True`, SSE) и разовый
режим ответа -- переключатель `stream` зеркалит одноимённый параметр ноды
и определяет, придёт ли `chat_node` один большой `Chunk` или последовательность
маленьких. Остальная логика ноды (сплиттер клауз, epoch-фенсинг) от этого
не зависит: она одинаково работает с редкими крупными и частыми мелкими
чанками.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import requests

_logger = logging.getLogger(__name__)

__all__ = [
    "Chunk",
    "EchoBackend",
    "LlamaCppBackend",
    "LlmBackend",
    "OpenAIBackend",
]

# Таймаут на установление соединения. Отдельный от request_timeout_s --
# зависший DNS/TCP-хендшейк не должен ждать полный бюджет на генерацию.
_CONNECT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Chunk:
    """Один кусок потока генерации."""

    text: str
    done: bool


class LlmBackend(Protocol):
    """Общий интерфейс бэкенда LLM."""

    def stream(self, messages: list[dict], abort: threading.Event) -> Iterator[Chunk]:
        """Отдать поток чанков. Обязан проверять `abort` и не блокироваться навечно."""

    def health(self) -> tuple[bool, str]:
        """Проверить доступность бэкенда. Возвращает (ok, detail)."""


class LlamaCppBackend:
    """HTTP-клиент OpenAI-совместимого `/v1/chat/completions`."""

    def __init__(
        self,
        base_url: str,
        model: str = "",
        max_tokens: int = 256,
        temperature: float = 0.7,
        stream: bool = True,
        request_timeout_s: float = 20.0,
    ) -> None:
        """Запомнить параметры соединения. Ничего не открывает заранее."""
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._stream = stream
        self._request_timeout_s = request_timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _payload(self, messages: list[dict]) -> dict[str, object]:
        payload: dict[str, object] = {
            "messages": messages,
            "stream": self._stream,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        if self._model:
            payload["model"] = self._model
        return payload

    def stream(self, messages: list[dict], abort: threading.Event) -> Iterator[Chunk]:
        """Запросить генерацию. `stream=True` -> разбор SSE построчно."""
        response = requests.post(
            f"{self._base_url}/v1/chat/completions",
            json=self._payload(messages),
            headers=self._headers(),
            stream=self._stream,
            timeout=(_CONNECT_TIMEOUT_S, self._request_timeout_s),
        )
        try:
            response.raise_for_status()
            if self._stream:
                yield from self._iter_sse(response, abort)
            else:
                yield self._parse_full(response.json())
        finally:
            response.close()

    def _iter_sse(self, response: object, abort: threading.Event) -> Iterator[Chunk]:
        # decode_unicode=True декодирует байты через response.encoding, а тот для
        # text/event-stream без charset в заголовке падает на ISO-8859-1 (дефолт
        # requests для text/*), а не на UTF-8 -- кириллица превращается в кракозябры.
        # Поэтому берём сырые байты и декодируем сами.
        for raw_bytes in response.iter_lines(decode_unicode=False):  # type: ignore[attr-defined]
            if abort.is_set():
                return
            if not raw_bytes:
                continue
            raw_line = raw_bytes.decode("utf-8", errors="replace")
            if not raw_line.startswith("data:"):
                continue
            data = raw_line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                _logger.warning("не удалось разобрать SSE-строку %r", raw_line)
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            text = (choices[0].get("delta") or {}).get("content") or ""
            if text:
                yield Chunk(text=text, done=False)
        yield Chunk(text="", done=True)

    def _parse_full(self, payload: dict) -> Chunk:
        choices = payload.get("choices") or []
        text = ""
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
        return Chunk(text=text, done=True)

    def health(self) -> tuple[bool, str]:
        """`GET /health`, при отказе -- `GET /v1/models`."""
        ok, detail = self._get_ok(f"{self._base_url}/health")
        if ok:
            return True, ""
        ok2, detail2 = self._get_ok(f"{self._base_url}/v1/models")
        if ok2:
            return True, ""
        return False, f"{detail}; {detail2}"

    def _get_ok(self, url: str) -> tuple[bool, str]:
        try:
            response = requests.get(url, headers=self._headers(), timeout=_CONNECT_TIMEOUT_S)
        except requests.RequestException as error:
            return False, str(error)
        if response.ok:
            return True, ""
        return False, f"HTTP {response.status_code}"


class OpenAIBackend(LlamaCppBackend):
    """Тот же протокол, что и `LlamaCppBackend`, плюс авторизация и обязательный `model`."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_env: str = "LLM_API_KEY",
        max_tokens: int = 256,
        temperature: float = 0.7,
        stream: bool = True,
        request_timeout_s: float = 20.0,
    ) -> None:
        """Создать бэкенд. Ключ читается из окружения, не из параметров ноды."""
        if not model:
            raise ValueError("OpenAIBackend требует непустой параметр model")
        super().__init__(
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=stream,
            request_timeout_s=request_timeout_s,
        )
        self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key:
            _logger.warning(
                "переменная окружения %s не задана -- запросы уйдут без авторизации",
                api_key_env,
            )

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers


class EchoBackend:
    """Детерминированный ответ без сети: аналог `NullBackend` из `guide_robot_voice`.

    Не эхо пользовательского текста -- заранее заданная фраза, отдаваемая
    по словам с фиксированной задержкой. Воспроизводимость (для тестов
    и Stage 0) важнее правдоподобия ответа.
    """

    def __init__(
        self,
        reply_text: str = "Здравствуйте! Чем могу помочь?",
        word_delay_s: float = 0.05,
    ) -> None:
        """Задать каноничный ответ и темп выдачи слов."""
        self._reply_text = reply_text
        self._word_delay_s = word_delay_s

    def stream(self, messages: list[dict], abort: threading.Event) -> Iterator[Chunk]:
        """Отдать `reply_text` по словам, обрываясь на `abort`."""
        del messages
        words = self._reply_text.split(" ")
        for index, word in enumerate(words):
            if abort.is_set():
                return
            text = word if index == 0 else " " + word
            yield Chunk(text=text, done=False)
            if self._word_delay_s > 0:
                time.sleep(self._word_delay_s)
        yield Chunk(text="", done=True)

    def health(self) -> tuple[bool, str]:
        """Всегда доступен -- сети нет."""
        return True, "ok"
