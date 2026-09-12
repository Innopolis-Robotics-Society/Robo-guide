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

from guide_robot_llm.llm_client.backend import Backend, CompletionResult
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError, BackendHTTPError

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
    backoff_s: float = 0.5,
) -> CompletionResult:
    """Пробовать `backends` по порядку, с retry внутри каждого.

    `BackendAborted` (barge-in) поднимается сразу наружу без ретраев --
    прерванный намеренно ход не ретраят ни на том же бэкенде, ни на
    следующем: посетитель уже не ждёт ответа на старый вопрос. Остальные
    ошибки (timeout/HTTP/сеть) -- до `backend.config.max_attempts` попыток НА
    ЭТОТ бэкенд (не общий на все, каждый бэкенд решает сам -- TASK_external_
    llm_backend.md §2, внешний шлюз с деньгами за попытку хочет `max_attempts=1`,
    локальный -- прежние 2) с паузой `backoff_s`, затем переход к следующему
    бэкенду. `BackendHTTPError` с кодом 4xx кроме 429 (401/403/404 и т.п. --
    неправильный ключ или конфиг, повтор того же запроса не поможет) не
    ретраится вовсе, сразу следующий бэкенд; 429 (rate limit) и 5xx ретраятся
    как обычно. Если исчерпаны все -- поднимается последняя пойманная ошибка
    (вызывающий, `dialog_agent`, решает как деградировать дальше).
    """
    if not backends:
        msg = "список бэкендов пуст"
        raise BackendError(msg)

    last_error: BackendError | None = None
    for backend in backends:
        max_attempts = backend.config.max_attempts
        for attempt in range(max_attempts):
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
                )
            except BackendAborted:
                raise
            except BackendHTTPError as error:
                last_error = error
                if 400 <= error.status_code < 500 and error.status_code != 429:
                    break  # неправильный запрос/ключ -- ретрай бессмыслен
                is_last_attempt_on_backend = attempt == max_attempts - 1
                if not is_last_attempt_on_backend:
                    time.sleep(backoff_s)
            except BackendError as error:
                last_error = error
                is_last_attempt_on_backend = attempt == max_attempts - 1
                if not is_last_attempt_on_backend:
                    time.sleep(backoff_s)

    if last_error is None:
        msg = "все бэкенды исчерпаны без ошибки -- недостижимо"
        raise BackendError(msg)
    raise last_error
