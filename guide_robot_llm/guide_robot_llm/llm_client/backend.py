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
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

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
    """Один бэкенд: адрес + раздельные таймауты + особенности внешних шлюзов.

    Помимо локального `llm_server/` (GBNF per-request), бэкенд может быть
    внешним OpenAI-совместимым шлюзом (Selectel AI Router и т.п.), который
    `model` в теле требует, GBNF не понимает, а reasoning-модели за ним не
    дают выключить рассуждения ни одним параметром (TASK_external_llm_backend.md
    §0) -- отсюда `structured`/`reasoning_budget_tokens`/`first_content_timeout_s`.
    """

    base_url: str  # "http://host:port/v1", без хвостового "/"
    api_key: str = ""  # пусто -- заголовок Authorization не шлём
    connect_timeout_s: float = 2.0
    read_timeout_s: float = 30.0
    name: str = ""  # для логов
    model: str = ""  # пусто -- ключ "model" не шлём
    structured: str = "gbnf"  # "gbnf" | "json_object" | "none"
    extra_body: Mapping[str, object] = field(default_factory=dict)
    # Reasoning тарифицируется и лимитируется как output (§0) -- прибавляется
    # к max_tokens фазы, иначе reasoning съедает весь бюджет и content обрезается.
    reasoning_budget_tokens: int = 0
    # Дедлайн на первый content-чанк -- reasoning-чанки держат сокет живым
    # (сбрасывают read_timeout_s), не защищая от бесконечного "думания".
    first_content_timeout_s: float | None = None
    max_attempts: int = 2  # заменяет глобальный max_attempts_per_backend
    # Держать в пуле готовое TCP+TLS соединение: SSE-ответ закрывается
    # недочитанным (шлюз держит стрим открытым после [DONE]), и соединение
    # умирает вместе с ним -- без прогрева каждый запрос платит рукопожатие.
    prewarm: bool = False


@dataclass
class CompletionResult:
    """Итог одного успешного вызова -- собранный текст + причина остановки от сервера."""

    text: str
    finish_reason: str = ""
    reasoning_chars: int = 0
    gateway_warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class Backend:
    """Один `base_url`. Синхронный вызов, всегда стримит внутри -- см. `complete()`."""

    def __init__(self, config: BackendConfig, *, session: requests.Session | None = None) -> None:
        """Запомнить конфиг; `session` подменяется в тестах (мок-сервер на localhost)."""
        self._config = config
        self._session = session or requests.Session()
        self._warming = threading.Lock()

    @property
    def config(self) -> BackendConfig:
        """Конфиг бэкенда, только для чтения (`ladder.py` читает `max_attempts`)."""
        return self._config

    def warm(self) -> None:
        """Открыть соединение заранее, в фоне: лёгкий GET, дочитанный до конца.

        Дочитанный ответ возвращает соединение в пул `requests.Session` --
        следующий `complete()` идёт по нему без TCP+TLS. Ошибки молча
        глотаются: прогрев -- оптимизация, не условие работы. Параллельные
        вызовы схлопываются в один.
        """
        if not self._config.prewarm or not self._warming.acquire(blocking=False):
            return
        threading.Thread(target=self._warm_blocking, name="llm-prewarm", daemon=True).start()

    def _warm_blocking(self) -> None:
        try:
            headers = {}
            if self._config.api_key:
                headers["Authorization"] = f"Bearer {self._config.api_key}"
            url = f"{self._config.base_url.rstrip('/')}/models"
            response = self._session.get(
                url, headers=headers, timeout=(self._config.connect_timeout_s, 5.0)
            )
            _ = response.content
        except requests.exceptions.RequestException:
            pass
        finally:
            self._warming.release()

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

        `config.extra_body` вливается в payload ПЕРВЫМ, core-ключи (`messages`/
        `max_tokens`/`temperature`/`stream`) идут поверх и не могут быть им
        перетёрты. `config.structured` решает, КАК передать `grammar`: локальный
        `llm_server/` понимает GBNF-грамматику per-request (`"gbnf"`), внешний
        OpenAI-совместимый шлюз -- нет, но соглашается на `response_format:
        {"type":"json_object"}` (`"json_object"`), а `"none"` -- ни то, ни
        другое (шлюз молча игнорирует незнакомые ключи, TASK_external_llm_
        backend.md §0). `max_tokens` в payload включает `config.
        reasoning_budget_tokens` -- у reasoning-моделей рассуждение
        тарифицируется как output и входит в тот же лимит.
        """
        payload: dict[str, object] = dict(self._config.extra_body)
        payload.update(
            {
                "messages": messages,
                "max_tokens": max_tokens + self._config.reasoning_budget_tokens,
                "temperature": temperature,
                "stream": True,
            }
        )
        if self._config.model:
            payload["model"] = self._config.model
        if grammar:
            if self._config.structured == "gbnf":
                payload["grammar"] = grammar
            elif self._config.structured == "json_object":
                payload["response_format"] = {"type": "json_object"}
            # "none" -- ни "grammar", ни "response_format": шлюз без GBNF и без
            # структурного гейта, у которого даже json_object не годится.
        if frequency_penalty is not None:
            payload["frequency_penalty"] = frequency_penalty

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

        return self._consume_stream(
            response, abort_event=abort_event, on_delta=on_delta, stop_when=stop_when
        )

    def _consume_stream(
        self,
        response: requests.Response,
        *,
        abort_event: threading.Event | None,
        on_delta: Callable[[str], None] | None,
        stop_when: Callable[[str], bool] | None = None,
    ) -> CompletionResult:
        """Разобрать SSE в текст + диагностику (reasoning/warnings/usage).

        `delta.reasoning`/`delta.reasoning_content` -- рассуждение
        reasoning-модели за внешним шлюзом: не идёт в `chunks`/`on_delta`/
        `stop_when` (это не ответ), только считается по длине --
        `_consume_stream` не решает, что с этим делать, это дело вызывающего
        (`dialog_agent_node` логирует WARN, TASK_external_llm_backend.md §4).

        `first_content_timeout_s` -- дедлайн на первый content-чанк, НЕЗАВИСИМЫЙ
        от `read_timeout_s` сокета: у reasoning-моделей за внешним шлюзом
        reasoning-чанки идут секундами и держат сокет живым (сбрасывают
        socket-level read timeout), не защищая от того, что content вообще не
        появится вовремя -- секундомер здесь свой, по `time.monotonic()`.
        """
        chunks: list[str] = []
        finish_reason = ""
        reasoning_chars = 0
        gateway_warnings: list[str] = []
        usage: dict = {}
        deadline = self._config.first_content_timeout_s
        start = time.monotonic()

        def _check_first_content_deadline() -> None:
            if chunks or deadline is None:
                return
            if time.monotonic() - start > deadline:
                msg = f"первый content не пришёл за {deadline}с (только reasoning/пусто)"
                raise BackendTimeout(msg)

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
                if "error" in event:
                    msg = f"ошибка от шлюза: {event['error']}"
                    raise BackendError(msg)
                warnings = (event.get("gateway") or {}).get("warnings") or []
                if warnings:
                    gateway_warnings.extend(warnings)
                event_usage = event.get("usage")
                if event_usage:
                    usage = event_usage
                choices = event.get("choices") or []
                if not choices:
                    _check_first_content_deadline()
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                reasoning_piece = delta.get("reasoning") or delta.get("reasoning_content") or ""
                if reasoning_piece:
                    reasoning_chars += len(reasoning_piece)
                content_piece = delta.get("content") or ""
                if content_piece:
                    chunks.append(content_piece)
                    if on_delta is not None:
                        on_delta(content_piece)
                    if stop_when is not None and stop_when("".join(chunks)):
                        finish_reason = finish_reason or "stop_when"
                        break
                reason = choice.get("finish_reason")
                if reason:
                    finish_reason = reason
                _check_first_content_deadline()
        except requests.exceptions.Timeout as error:
            raise BackendTimeout(str(error)) from error
        except requests.exceptions.RequestException as error:
            raise BackendError(str(error)) from error
        finally:
            response.close()
            # Недочитанный стрим закрыт вместе с соединением -- готовим
            # следующее, пока исполняется действие или говорит TTS.
            self.warm()
        return CompletionResult(
            text="".join(chunks),
            finish_reason=finish_reason,
            reasoning_chars=reasoning_chars,
            gateway_warnings=gateway_warnings,
            usage=usage,
        )
