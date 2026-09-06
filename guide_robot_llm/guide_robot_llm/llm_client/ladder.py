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
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError

__all__ = ["complete_with_fallback"]


def complete_with_fallback(
    backends: Sequence[Backend],
    messages: list[dict],
    *,
    grammar: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
    abort_event: threading.Event | None = None,
    on_delta: Callable[[str], None] | None = None,
    max_attempts_per_backend: int = 2,
    backoff_s: float = 0.5,
) -> CompletionResult:
    """Пробовать `backends` по порядку, с retry внутри каждого.

    `BackendAborted` (barge-in) поднимается сразу наружу без ретраев --
    прерванный намеренно ход не ретраят ни на том же бэкенде, ни на
    следующем: посетитель уже не ждёт ответа на старый вопрос. Остальные
    ошибки (timeout/HTTP/сеть) -- до `max_attempts_per_backend` попыток на
    бэкенд с паузой `backoff_s`, затем переход к следующему бэкенду. Если
    исчерпаны все -- поднимается последняя пойманная ошибка (вызывающий,
    `dialog_agent` в шаге 5, решает как деградировать дальше).
    """
    if not backends:
        msg = "список бэкендов пуст"
        raise BackendError(msg)

    last_error: BackendError | None = None
    for backend in backends:
        for attempt in range(max_attempts_per_backend):
            try:
                return backend.complete(
                    messages,
                    grammar=grammar,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    abort_event=abort_event,
                    on_delta=on_delta,
                )
            except BackendAborted:
                raise
            except BackendError as error:
                last_error = error
                is_last_attempt_on_backend = attempt == max_attempts_per_backend - 1
                if not is_last_attempt_on_backend:
                    time.sleep(backoff_s)

    if last_error is None:
        msg = "все бэкенды исчерпаны без ошибки -- недостижимо"
        raise BackendError(msg)
    raise last_error
