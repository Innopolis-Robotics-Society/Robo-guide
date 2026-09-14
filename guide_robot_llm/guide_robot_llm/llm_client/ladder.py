"""Лестница деградации: список бэкендов, retry/backoff, без stateful circuit breaker.

Осознанно упрощено против `llm_server/iros_llm_server_SPEC.md` §0 ("список
бэкендов и circuit breaker"): сегодня реально развёрнут один сервер (один
контейнер, одна модель за раз -- профили `qwen7b-q4`/`cpu-fallback`
переключаются вручную через `.env`, не работают параллельно как два живых
эндпоинта). Полноценный circuit breaker с cooldown-таймерами нечего сейчас
резервировать -- это переинжиниринг вперёд задачи. Если появится второй
живой бэкенд и понадобится "не пробовать N секунд после серии отказов" --
отдельная, явно заказанная доработка этого модуля, а не блокер шага 4.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence

from guide_robot_llm.llm_client.backend import Backend, CompletionResult, has_images
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.llm_client.telemetry import ClientTelemetry

__all__ = ["complete_with_fallback"]


def complete_with_fallback(
    backends: Sequence[Backend],
    messages: list[dict],
    *,
    grammar: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    frequency_penalty: float | None = None,
    abort_event: threading.Event | None = None,
    on_delta: Callable[[str], None] | None = None,
    stop_when: Callable[[str], bool] | None = None,
    max_attempts_per_backend: int = 2,
    backoff_s: float = 0.5,
    telemetry: ClientTelemetry | None = None,
) -> CompletionResult:
    """Пробовать `backends` по порядку, с retry внутри каждого.

    `BackendAborted` (barge-in) поднимается сразу наружу без ретраев --
    прерванный намеренно ход не ретраят ни на том же бэкенде, ни на
    следующем: посетитель уже не ждёт ответа на старый вопрос. Остальные
    ошибки (timeout/HTTP/сеть) -- до `max_attempts_per_backend` попыток на
    бэкенд с паузой `backoff_s`, затем переход к следующему бэкенду. Если
    исчерпаны все -- поднимается последняя пойманная ошибка (вызывающий,
    `dialog_agent` в шаге 5, решает как деградировать дальше).

    Taiga #3: capability-проверка -- запрос с кадрами не отправляется на
    эндпоинт с `multimodal_enabled=false`: такой бэкенд пропускается сразу
    (без попыток, не как отказ), а fallback может уйти с недоступного
    VLM-эндпоинта на другой совместимый. `telemetry` (опционально) считает
    попытки/ретраи/фолбэки/пропуски; отказы со стадией (`network`/
    `generation`) и тайминги пишет `Backend.complete()` в тот же объект.
    """
    if not backends:
        msg = "список бэкендов пуст"
        raise BackendError(msg)

    needs_multimodal = has_images(messages)
    last_error: BackendError | None = None
    for backend in backends:
        if needs_multimodal and not backend.config.multimodal_enabled:
            # Несовместим по capability: HTTP-попытка бессмысленна (сервер
            # всё равно не примет image_url), ретраить и "считать отказом" не
            # нужно -- только счётчик пропуска.
            if telemetry is not None:
                telemetry.record_skipped_incompatible()
            continue
        for attempt in range(max_attempts_per_backend):
            if telemetry is not None:
                telemetry.record_attempt()
                if attempt > 0:
                    telemetry.record_retry()
            try:
                return backend.complete(
                    messages,
                    grammar=grammar,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    frequency_penalty=frequency_penalty,
                    abort_event=abort_event,
                    on_delta=on_delta,
                    stop_when=stop_when,
                    telemetry=telemetry,
                )
            except BackendAborted:
                raise
            except BackendError as error:
                last_error = error
                is_last_attempt_on_backend = attempt == max_attempts_per_backend - 1
                if not is_last_attempt_on_backend:
                    time.sleep(backoff_s)
        if telemetry is not None and backend is not backends[-1]:
            # Фолбэк -- только если дальше есть, куда идти; у последнего
            # бэкенда это просто исчерпание цепочки.
            telemetry.record_fallback()

    if last_error is None:
        msg = "все бэкенды несовместимы с запросом или исчерпаны без ошибки"
        raise BackendError(msg)
    raise last_error
