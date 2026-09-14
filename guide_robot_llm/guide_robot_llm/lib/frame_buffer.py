"""Кольцевой буфер сжатых кадров для хода диалога (Taiga #2).

Чистая логика без rclpy -- `dialog_agent_node.py` сам берёт время из своей
`get_clock()` (в тестах это sim-часы) и передаёт кадры/моменты явно; модуль
тестируется на голых байтах JPEG без ROS (дизайн-конвенция пакета: `lib/`).

Контракт (issue #2, epic #14):

- `offer(jpeg, captured_at)` -- вызывается из ROS-колбэка подписчика
  `/camera/image_raw/compressed` (BEST_EFFORT, depth 1). Кадр проходит
  проверки: JPEG-заголовок + decode (коррупт -- отброс и счётчик),
  payload-потолок (oversize -- отброс и счётчик), даунскейл до
  `max_long_edge_px` с перекодированием `jpeg_quality`, если длинная сторона
  больше. Кольцо ограничено: кадры старше окна выкидываются, при переполнении
  -- самые старые (память не растёт от fps камеры).
- `freeze(now)` -- вызывается на моменте транскрипта. НЕ деструктивен (кольцо
  продолжает жить для следующего хода): возвращает до `frame_count` кадров,
  равномерно разнесённых по окну `lookback_s`, не старше `max_frame_age_s`,
  с совокупным payload не более `max_payload_bytes` (при превышении выкидывают
  с самого старого). Кадры -- data-URL `data:image/jpeg;base64,...`: такая
  форма приходит в `llm_client.build_content()` (Taiga #3) и в снимок хода
  `snap["frames"]` (консумер -- #4).
- Без свежих кадров `freeze` возвращает `[]` -- ход идёт text-only
  (failure handling из issue: отсутствие камеры не ломает ни lifecycle, ни
  диалог). Никаких записей кадров на диск в этом модуле нет и не будет
  (out-of-scope issue #2).

Потоки: offer (reentrant callback-группа) и freeze (поток хода) идут
параллельно -- одна блокировка; PIL-decode вне её (медленнее кольцевых
операций), счётчики отбросов обновляются под той же блокировкой.
"""

from __future__ import annotations

import base64
import io
import logging
import threading
from dataclasses import dataclass

from PIL import Image

__all__ = ["FrozenFrame", "FrameBuffer"]

logger = logging.getLogger(__name__)

_DATA_URL_PREFIX = "data:image/jpeg;base64,"
_JPEG_SOI = b"\xff\xd8\xff"  # SOI + начало APPn, чего достаточно для отсева мусора
_KEEP = 0
_CORRUPT = 1
_OVERSIZED = 2


@dataclass(frozen=True)
class FrozenFrame:
    """Замороженный кадр: data-URL для ЛЛМ + метаданные для диагностики."""

    data_url: str
    captured_at: float
    payload_bytes: int
    width: int = 0
    height: int = 0


class FrameBuffer:
    """Ограниченное кольцо JPEG-кадров: offer из callback'а, freeze на транскрипте."""

    def __init__(
        self,
        *,
        frame_count: int = 3,
        lookback_s: float = 2.0,
        max_frame_age_s: float = 2.0,
        max_long_edge_px: int = 1280,
        max_payload_bytes: int = 2_500_000,
        jpeg_quality: int = 80,
        max_buffered_frames: int = 64,
    ) -> None:
        """Запомнить бюджет; `max_buffered_frames` -- жёсткий потолок памяти кольца."""
        if frame_count < 0:
            msg = "frame_count < 0"
            raise ValueError(msg)
        if max_buffered_frames < 1:
            msg = "max_buffered_frames < 1"
            raise ValueError(msg)
        self._frame_count = frame_count
        self._lookback_s = float(lookback_s)
        self._max_frame_age_s = float(max_frame_age_s)
        self._max_long_edge_px = int(max_long_edge_px)
        self._max_payload_bytes = int(max_payload_bytes)
        self._jpeg_quality = int(jpeg_quality)
        self._capacity = int(max_buffered_frames)
        self._frames: list[tuple[bytes, float]] = []  # (jpeg, captured_at)
        self._lock = threading.Lock()
        self._discarded_corrupt = 0
        self._discarded_oversized = 0

    # -- offer ---------------------------------------------------------------

    def offer(self, jpeg: bytes, captured_at: float) -> None:
        """Принять кадр: валидация + даунскейл, затем в кольцо (с выкидыванием старых).

        Отброшенный кадр -- не ошибка: метод молча возвращает, счётчик
        в `stats` отражает отброс.
        """
        prepared = self._prepare(jpeg)
        with self._lock:
            if prepared is _CORRUPT:
                self._discarded_corrupt += 1
                logger.debug("frame: не JPEG / decode не прошёл -- отброшен")
                return
            if prepared is _OVERSIZED:
                self._discarded_oversized += 1
                logger.debug("frame: payload > %d B -- отброшен", self._max_payload_bytes)
                return
            self._frames.append((prepared, captured_at))
            # Выкидываем то, что старше окна, и то, что раздуло кольцо.
            horizon = captured_at - min(self._lookback_s, self._max_frame_age_s)
            while self._frames and self._frames[0][1] < horizon:
                self._frames.pop(0)
            while len(self._frames) > self._capacity:
                self._frames.pop(0)

    def _prepare(self, jpeg: bytes) -> bytes | int:
        """Валидация + даунскейл; возвращает байты либо причину отброса (сентинел)."""
        if len(jpeg) < 3 or jpeg[:3] != _JPEG_SOI:
            return _CORRUPT
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                image.load()  # декодируем целиком -- обрезанный файл падает здесь
                width, height = image.size
        except Exception:  # noqa: BLE001 -- любой сбой decode == коррупт
            return _CORRUPT

        if max(width, height) > self._max_long_edge_px:
            scale = self._max_long_edge_px / max(width, height)
            with Image.open(io.BytesIO(jpeg)) as source:
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                resized = source.resize(new_size, Image.LANCZOS)
                buffer = io.BytesIO()
                resized.save(buffer, format="JPEG", quality=self._jpeg_quality)
            jpeg = buffer.getvalue()
        if len(jpeg) > self._max_payload_bytes:
            return _OVERSIZED
        return jpeg

    # -- freeze ---------------------------------------------------------------

    def freeze(self, now: float) -> list[FrozenFrame]:
        """Заморозить до `frame_count` свежих кадров (не деструктивно)."""
        with self._lock:
            eligible = [
                (jpeg, ts)
                for jpeg, ts in self._frames
                if now - ts <= self._max_frame_age_s and now - ts <= self._lookback_s
            ]
            eligible.sort(key=lambda item: item[1])  # offer'ы могут прийти не по порядку
            selected = self._sample(eligible)
            # Бюджет совокупного payload: выкидываем с самого старого.
            while selected and sum(len(jpeg) for jpeg, _ in selected) > self._max_payload_bytes:
                selected = selected[1:]
            return [
                FrozenFrame(
                    data_url=_DATA_URL_PREFIX + base64.b64encode(jpeg).decode("ascii"),
                    captured_at=ts,
                    payload_bytes=len(jpeg),
                    width=self._dims(jpeg)[0],
                    height=self._dims(jpeg)[1],
                )
                for jpeg, ts in selected
            ]

    @staticmethod
    def _dims(jpeg: bytes) -> tuple[int, int]:
        try:
            with Image.open(io.BytesIO(jpeg)) as image:
                return int(image.width), int(image.height)
        except Exception:  # noqa: BLE001 -- размер не критичен для лога
            return 0, 0

    def _sample(self, eligible: list[tuple[bytes, float]]) -> list[tuple[bytes, float]]:
        """До `frame_count` кадров с равным шагом по времени (крайние включительно)."""
        count = len(eligible)
        if count == 0 or self._frame_count == 0:
            return []
        if count <= self._frame_count:
            return list(eligible)
        if self._frame_count == 1:
            return [eligible[-1]]  # только самый свежий
        return [
            eligible[round(i * (count - 1) / (self._frame_count - 1))]
            for i in range(self._frame_count)
        ]

    # -- диагностика -----------------------------------------------------------

    def size(self) -> int:
        """Сколько кадров сейчас в кольце (для тестов/диагностики)."""
        with self._lock:
            return len(self._frames)

    @property
    def stats(self) -> dict[str, int]:
        """Накопительные счётчики отбросов -- node логирует их, если ненулевые."""
        with self._lock:
            return {
                "discarded_corrupt": self._discarded_corrupt,
                "discarded_oversized": self._discarded_oversized,
            }
