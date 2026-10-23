"""Окно снимка навигации по событию: кольцевой буфер + захват вокруг триггера.

Чистая логика, без ROS -- nav_snapshot_node кормит сюда сырые (уже
сериализованные) сообщения и время, а test_snapshot_window.py проверяет ту же
логику без DDS.

Зачем: 2026-10-23 робот ~73 с стоял в recovery посреди зала, Spin каждый раз
выходил по "Collision Ahead", а скан тех минут нигде не сохранился -- по логам
было не понять, что видел лидар. Постоянный rosbag на Orin слишком дорог
(0.5-2 ГБ/ч и ещё подписчик на скан), поэтому в памяти держатся последние
`pre_s` секунд, а на диск уходит только окно вокруг recovery.

Повторный триггер во время захвата не открывает новый снимок, а продлевает
текущий (один эпизод "застрял" -- один снимок), но не дальше `max_s` от начала
захвата: иначе робот, бесконечно крутящий recovery, писал бы без конца.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

__all__ = ["Record", "Snapshot", "SnapshotWindow"]


@dataclass(frozen=True)
class Record:
    """Одно сообщение: топик, время приёма (нс) и сериализованные байты CDR."""

    topic: str
    t_ns: int
    data: bytes


@dataclass
class Snapshot:
    """Готовый к записи снимок."""

    reason: str
    trigger_ns: int
    end_ns: int
    triggers: int
    records: list[Record]


@dataclass
class SnapshotWindow:
    """Кольцевой буфер последних `pre_s` секунд и захват `post_s` после триггера.

    Время -- целые наносекунды (часы узла, в т.ч. под use_sim_time).
    """

    pre_s: float
    post_s: float
    max_s: float
    _ring: deque[Record] = field(default_factory=deque, init=False)
    _capture: list[Record] | None = field(default=None, init=False)
    _start_ns: int = field(default=0, init=False)
    _end_ns: int = field(default=0, init=False)
    _reason: str = field(default="", init=False)
    _triggers: int = field(default=0, init=False)

    @property
    def capturing(self) -> bool:
        """Идёт ли сейчас захват."""
        return self._capture is not None

    def add(self, record: Record) -> None:
        """Положить сообщение в буфер (и в захват, если он идёт)."""
        if self._capture is not None:
            self._capture.append(record)
            return
        self._ring.append(record)
        horizon = record.t_ns - int(self.pre_s * 1e9)
        while self._ring and self._ring[0].t_ns < horizon:
            self._ring.popleft()

    def trigger(self, *, now_ns: int, reason: str) -> bool:
        """Начать захват или продлить текущий. True -- если начат новый снимок."""
        if self._capture is None:
            self._capture = list(self._ring)
            self._ring.clear()
            self._start_ns = now_ns
            self._end_ns = now_ns + int(self.post_s * 1e9)
            self._reason = reason
            self._triggers = 1
            return True
        self._triggers += 1
        limit = self._start_ns + int(self.max_s * 1e9)
        self._end_ns = min(max(self._end_ns, now_ns + int(self.post_s * 1e9)), limit)
        return False

    def poll(self, *, now_ns: int) -> Snapshot | None:
        """Вернуть завершённый снимок, когда окно после триггера истекло."""
        if self._capture is None or now_ns < self._end_ns:
            return None
        snapshot = Snapshot(
            reason=self._reason,
            trigger_ns=self._start_ns,
            end_ns=self._end_ns,
            triggers=self._triggers,
            records=self._capture,
        )
        self._capture = None
        return snapshot
