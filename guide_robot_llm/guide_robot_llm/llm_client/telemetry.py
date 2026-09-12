"""Клиентская телеметрия вызова `llm_client` (Taiga #3).

Issue #3 требует "сырых" таймингов и счётчиков на стороне клиента
(interaction log v6 из #8 их потом разовьёт в метрики, а #11 -- в
сервинг-метрики матрицы):

- тайминги стадий одного вызова: serialization / upload / TTFT / full /
  parse-ready (см. `StageTimings`);
- счётчики отказов: HTTP-ошибки, таймауты, фолбэки, ретраи,
  пропуски несовместимых по capability эндпоинтов;
- атрибуция отказов по стадии: `network` (до первого байта ответа) или
  `generation` (после начала генерации) -- чтобы #11 мог раскладывать
  отказы по стадиям, не угадывая.

Чистый модуль без `rclpy` и `requests`: только `threading` и `time`
(монотонные метки `complete()` передаёт уже посчитанными в миллисекундах)
-- тестируется без сети, как остальной `llm_client`.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass

__all__ = ["STAGE_NETWORK", "STAGE_GENERATION", "StageTimings", "ClientTelemetry"]

#: Отказ случился до первого байта ответа (коннект, отправка, ожидание).
STAGE_NETWORK = "network"
#: Отказ случился после начала генерации (битый SSE, обрыв серединой).
STAGE_GENERATION = "generation"


@dataclass
class StageTimings:
    """Тайминги одного вызова в миллисекундах; `None` -- стадия не достигнута.

    `upload_ms` -- отправка запроса до первого байта ответа (TTFB, включает
    коннект и ожидание сервера); `ttft_ms` -- до первого непустого токена;
    `parse_ready_ms` -- до момента, когда накопленный текст удовлетворил
    `stop_when` (иначе совпадает с `full_ms`).
    """

    serialization_ms: float | None = None
    upload_ms: float | None = None
    ttft_ms: float | None = None
    full_ms: float | None = None
    parse_ready_ms: float | None = None


class ClientTelemetry:
    """Потокобезопасный аккумулятор таймингов и счётчиков для цепочки бэкендов.

    Один экземпляр живёт на время хода (`dialog_agent` создаёт и передаёт в
    `complete_with_fallback`); `snapshot()` -- плоский `dict`, пригодный для
    jsonl-лога и для агрегации по #11. Счётчики кумулятивные: попытка,
    ушедшая в ретрай/фолбэк, остаётся в счётчиках после успешного завершения.
    """

    def __init__(self) -> None:
        """Пустой аккумулятор: все счётчики 0, таймингов нет."""
        self._lock = threading.Lock()
        self._attempts = 0
        self._retries = 0
        self._fallbacks = 0
        self._skipped_incompatible = 0
        self._http_failures = 0
        self._timeouts = 0
        self._failure_stages: dict[str, int] = {}
        self._timings: list[StageTimings] = []
        self._last_failure_stage: str | None = None
        self._last_failure_kind: str | None = None

    def record_attempt(self) -> None:
        """Каждый вызов `Backend.complete()` (включая ретраи)."""
        with self._lock:
            self._attempts += 1

    def record_retry(self) -> None:
        """Повторная попытка на том же бэкенде."""
        with self._lock:
            self._retries += 1

    def record_fallback(self) -> None:
        """Переход к следующему бэкенду цепочки."""
        with self._lock:
            self._fallbacks += 1

    def record_skipped_incompatible(self) -> None:
        """Бэкенд пропущен без попыток: несовместим по capability (issue #3)."""
        with self._lock:
            self._skipped_incompatible += 1

    def record_http_failure(self, stage: str) -> None:
        """HTTP-ошибка (не 2xx) на стадии `stage`."""
        with self._lock:
            self._http_failures += 1
            self._failure_stages[stage] = self._failure_stages.get(stage, 0) + 1
            self._last_failure_stage = stage
            self._last_failure_kind = "http"

    def record_timeout(self, stage: str) -> None:
        """Таймаут (connect/read) на стадии `stage`."""
        with self._lock:
            self._timeouts += 1
            self._failure_stages[stage] = self._failure_stages.get(stage, 0) + 1
            self._last_failure_stage = stage
            self._last_failure_kind = "timeout"

    def record_error(self, stage: str) -> None:
        """Прочий отказ (`BackendError`/`BackendAborted`) на стадии `stage`."""
        with self._lock:
            self._failure_stages[stage] = self._failure_stages.get(stage, 0) + 1
            self._last_failure_stage = stage
            self._last_failure_kind = "error"

    def record_success(self, timings: StageTimings) -> None:
        """Успешный вызов с таймингами стадий."""
        with self._lock:
            self._timings.append(timings)

    def snapshot(self) -> dict:
        """Плоская копия текущего состояния (без блокировки на чтение вне)."""
        with self._lock:
            return {
                "attempts": self._attempts,
                "retries": self._retries,
                "fallbacks": self._fallbacks,
                "skipped_incompatible": self._skipped_incompatible,
                "http_failures": self._http_failures,
                "timeouts": self._timeouts,
                "failure_stages": dict(self._failure_stages),
                "last_failure": (
                    {
                        "stage": self._last_failure_stage,
                        "kind": self._last_failure_kind,
                    }
                    if self._last_failure_stage is not None
                    else None
                ),
                "timings": [asdict(t) for t in self._timings],
            }

    def reset(self) -> None:
        """Сбросить все счётчики и тайминги (повторное использование объекта)."""
        with self._lock:
            self._attempts = 0
            self._retries = 0
            self._fallbacks = 0
            self._skipped_incompatible = 0
            self._http_failures = 0
            self._timeouts = 0
            self._failure_stages = {}
            self._timings = []
            self._last_failure_stage = None
            self._last_failure_kind = None
