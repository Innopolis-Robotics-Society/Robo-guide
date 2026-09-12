"""Один HTTP-бэкенд поверх OpenAI-совместимого `/v1/chat/completions` (llm_plam.md §4).

Контракт сервера -- `llm_server/iros_llm_server_SPEC.md` §0/§6: стриминг SSE,
GBNF передаётся per-request в теле (не файлом на сервере), раздельные
connect/read таймауты (сеть локальная -- коннект быстрый, генерация идёт
секундами). Этот модуль -- только транспорт: как собрать `messages` (system
prompt, история, снапшот, порядок статика-перед-волатильным для
`CACHE_REUSE`) -- дело вызывающего (`dialog_agent`, шаг 5), здесь `messages`
просто пересылаются как дали.

Taiga #3: capability-конфигурация эндпоинта (модель, дополнительные
заголовки, multimodal, лимит кадров), content-массивы с `image_url` для
мультимодальных запросов, тайминги стадий в `ClientTelemetry` (serialization
/ upload / TTFT / full / parse-ready) и отказ по стадии (network -- до
первого байта ответа, generation -- после).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import requests

from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)
from guide_robot_llm.llm_client.telemetry import (
    STAGE_GENERATION,
    STAGE_NETWORK,
    ClientTelemetry,
    StageTimings,
)

__all__ = [
    "Backend",
    "BackendConfig",
    "CompletionResult",
    "build_content",
    "count_images",
    "has_images",
]

_DONE = "[DONE]"


@dataclass(frozen=True)
class BackendConfig:
    """Один бэкенд: адрес + раздельные таймауты + capability-конфиг (Taiga #3)."""

    base_url: str  # "http://host:port/v1", без хвостового "/"
    api_key: str = ""  # пусто -- заголовок Authorization не шлём
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 30.0
    # Taiga #3: capability-конфигурация эндпоинта. Дефолты сохраняют
    # прежнее поведение: text-only эндпоинт, без ключа `model` в payload,
    # без дополнительных заголовков.
    model_name: str = ""  # пусто -- ключ `model` в payload не идёт
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    multimodal_enabled: bool = False  # принимает ли эндпоинт image_url-parts
    max_images: int = 0  # максимум кадров в одном запросе; 0 -- без лимита


@dataclass
class CompletionResult:
    """Итог одного успешного вызова -- собранный текст + причина остановки от сервера."""

    text: str
    finish_reason: str = ""


def build_content(text: str, frames: Sequence[str] = ()) -> str | list[dict]:
    """`content` сообщения с опциональными кадрами (Taiga #3).

    Без кадров -- обычная строка: text-only запросы остаются байт-в-байт
    прежними, и серверу не нужно ничего различать. С кадрами -- content-
    массив: text-part первым, затем `image_url`-parts с data-URL (кадры
    приходят уже в форме `data:image/...;base64,...` -- сборка data-URL из
    JPEG -- дело вызывающего, #2/#4).
    """
    if not frames:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    parts.extend({"type": "image_url", "image_url": {"url": frame}} for frame in frames)
    return parts


def count_images(messages: list[dict]) -> int:
    """Сколько `image_url`-parts во всех сообщениях (0 для text-only)."""
    count = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                count += 1
    return count


def has_images(messages: list[dict]) -> bool:
    """Есть ли кадры в запросе -- ladder использует для capability-проверки."""
    return count_images(messages) > 0


class Backend:
    """Один `base_url`. Синхронный вызов, всегда стримит внутри -- см. `complete()`."""

    def __init__(self, config: BackendConfig, *, session: requests.Session | None = None) -> None:
        """Запомнить конфиг; `session` подменяется в тестах (мок-сервер на localhost)."""
        self._config = config
        self._session = session or requests.Session()

    @property
    def config(self) -> BackendConfig:
        """Конфиг бэкенда (capability-проверка в `ladder`, Taiga #3)."""
        return self._config

    def complete(
        self,
        messages: list[dict],
        *,
        grammar: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.2,
        frequency_penalty: float | None = None,
        abort_event: threading.Event | None = None,
        on_delta: Callable[[str], None] | None = None,
        stop_when: Callable[[str], bool] | None = None,
        telemetry: ClientTelemetry | None = None,
    ) -> CompletionResult:
        """POST `.../chat/completions` со `stream=true`, разобрать SSE, собрать полный текст.

        Стрим -- не опция, а необходимость: `requests` не даёт прервать уже
        начатый блокирующий (нестримящий) вызов из другого потока, а abort по
        barge-in (llm_plam.md §6: "abort HTTP-запроса, не просто игнор
        ответа") обязан реально закрывать соединение, не имитацией. Между
        чанками -- единственная точка, где можно проверить `abort_event` и
        оборвать генерацию на сервере, не дожидаясь остатка.

        `stop_when(text)` -- ранняя остановка без `BackendAborted`: как только
        накопленный текст удовлетворяет предикату (валидный tool-call JSON),
        стрим рвётся и возвращается то, что уже есть. Не путать с barge-in.

        `read_timeout_s` в `requests` -- таймаут между чтениями сокета, не на
        весь ответ целиком: пока сервер шлёт дельты с паузами короче
        `read_timeout_s`, многосекундная генерация не заденет его.

        `frequency_penalty` (stage5 п.3) -- только для фазы реплики
        (вызывающий не передаёт его для фазы действия: там грамматика и
        temperature 0, штраф повторов там не нужен и не проверялся). `None`
        -- ключ не идёт в payload вовсе, а не `0.0`: сервер, которому
        параметр незнаком, не обязан отличать "выключено" от "не прислали".

        Taiga #3: кадры в `messages` (content-массивы) прогоняются через
        capability-проверки -- запрос с кадрами не уходит на эндпоинт с
        `multimodal_enabled=false` и не превышает `max_images` (отказ до
        HTTP, не после). `telemetry` получает тайминги стадий и отказы с
        атрибуцией: до первого байта ответа -- `network`, после --
        `generation`.
        """
        images = count_images(messages)
        if images and not self._config.multimodal_enabled:
            msg = (
                f"endpoint {self._config.base_url} не принимает кадры "
                "(multimodal_enabled=false)"
            )
            raise BackendError(msg)
        if images and self._config.max_images and images > self._config.max_images:
            msg = f"кадров {images} -- больше лимита эндпоинта ({self._config.max_images})"
            raise BackendError(msg)

        t_start = time.monotonic()
        payload: dict[str, object] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if self._config.model_name:
            payload["model"] = self._config.model_name
        if grammar:
            payload["grammar"] = grammar
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty

        headers = {"Content-Type": "application/json"}
        headers.update(self._config.extra_headers)
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        timeout = (self._config.connect_timeout_s, self._config.read_timeout_s)
        t_request_start = time.monotonic()
        timings = StageTimings(serialization_ms=(t_request_start - t_start) * 1000.0)

        try:
            response = self._session.post(
                url, json=payload, headers=headers, timeout=timeout, stream=True
            )
        except requests.exceptions.Timeout as error:
            if telemetry is not None:
                telemetry.record_timeout(STAGE_NETWORK)
            raise BackendTimeout(str(error)) from error
        except requests.exceptions.RequestException as error:
            if telemetry is not None:
                telemetry.record_error(STAGE_NETWORK)
            raise BackendError(str(error)) from error
        # upload/send: отправка до первого байта (заголовков) ответа -- TTFB.
        timings.upload_ms = (time.monotonic() - t_request_start) * 1000.0

        if response.status_code != 200:
            if telemetry is not None:
                telemetry.record_http_failure(STAGE_NETWORK)
            body = response.text
            response.close()
            raise BackendHTTPError(response.status_code, body)

        return self._consume_stream(
            response,
            abort_event=abort_event,
            on_delta=on_delta,
            stop_when=stop_when,
            telemetry=telemetry,
            timings=timings,
            t_request_start=t_request_start,
        )

    def _consume_stream(
        self,
        response: requests.Response,
        *,
        abort_event: threading.Event | None,
        on_delta: Callable[[str], None] | None,
        stop_when: Callable[[str], bool] | None = None,
        telemetry: ClientTelemetry | None = None,
        timings: StageTimings | None = None,
        t_request_start: float | None = None,
    ) -> CompletionResult:
        """Чтение SSE-потока до конца ответа.

        До сюда первый байт ответа уже есть, поэтому каждый отказ здесь --
        стадия `generation` (Taiga #3).
        """
        chunks: list[str] = []
        finish_reason = ""
        first_token_at: float | None = None
        parse_ready_at: float | None = None
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
                    if telemetry is not None:
                        telemetry.record_error(STAGE_GENERATION)
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
                    if telemetry is not None:
                        telemetry.record_error(STAGE_GENERATION)
                    msg = f"битый JSON в SSE: {data[:200]!r}"
                    raise BackendError(msg) from error
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = (choice.get("delta") or {}).get("content") or ""
                if delta:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                    chunks.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
                    if stop_when is not None and stop_when("".join(chunks)):
                        # Parse-ready: накопленный текст удовлетворил предикат
                        # (валидный action JSON) -- именно этот момент меряет
                        # #11 (JSON-ready action latency).
                        if parse_ready_at is None:
                            parse_ready_at = time.monotonic()
                        finish_reason = finish_reason or "stop_when"
                        break
                reason = choice.get("finish_reason")
                if reason:
                    finish_reason = reason
        except requests.exceptions.Timeout as error:
            if telemetry is not None:
                telemetry.record_timeout(STAGE_GENERATION)
            raise BackendTimeout(str(error)) from error
        except requests.exceptions.RequestException as error:
            if telemetry is not None:
                telemetry.record_error(STAGE_GENERATION)
            raise BackendError(str(error)) from error
        finally:
            response.close()
        # Тайминги стадий: TTFT -- до первого непустого токена, full -- до
        # конца стрима ([DONE]/stop_when/конец), parse_ready -- до срабатывания
        # stop_when (не сработал -- совпадает с full). База -- старт отправки.
        if timings is not None and t_request_start is not None:
            t_end = time.monotonic()
            if first_token_at is not None:
                timings.ttft_ms = (first_token_at - t_request_start) * 1000.0
            timings.full_ms = (t_end - t_request_start) * 1000.0
            if parse_ready_at is not None:
                timings.parse_ready_ms = (parse_ready_at - t_request_start) * 1000.0
            else:
                timings.parse_ready_ms = timings.full_ms
            if telemetry is not None:
                telemetry.record_success(timings)
        return CompletionResult(text="".join(chunks), finish_reason=finish_reason)
