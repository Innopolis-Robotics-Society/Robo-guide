"""Учёт присутствия посетителя по разрозненным свидетельствам (design §6).

Чистая логика, без ROS -- по тем же причинам, что и resume.py/chunk_plan.py
(guide_robot_mission_control/resume.py): presence_monitor_node и
test_presence.py обязаны сходиться в одном и том же ответе на "присутствует
ли посетитель прямо сейчас", а единственный надёжный способ это
гарантировать -- не давать решению жить внутри узла.

`present` взводится НЕМЕДЛЕННО любым принятым свидетельством и снимается
только после `disengage_timeout_s` без новых -- то есть `present` не то же
самое, что "только что было свидетельство"; для этого есть
`seconds_since_evidence`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["PresenceTracker", "vad_evidence_allowed"]


@dataclass
class PresenceTracker:
    """Одна временная шкала последнего засчитанного свидетельства.

    Время -- в наносекундах монотонного/симулируемого источника (nanoseconds
    от `rclpy.time.Time`), не datetime: presence_monitor обязан работать
    под `use_sim_time` так же, как и под настоящими часами, а сравнение
    целых наносекунд не зависит от того, откуда они взялись.
    """

    disengage_timeout_s: float
    _last_evidence_ns: int | None = field(default=None, init=False)
    _last_source: str = field(default="", init=False)

    def record_evidence(self, *, now_ns: int, source: str) -> None:
        """Засчитать свидетельство. Более старое (now_ns меньше уже известного) игнорируется."""
        if self._last_evidence_ns is not None and now_ns < self._last_evidence_ns:
            return
        self._last_evidence_ns = now_ns
        self._last_source = source

    def present(self, *, now_ns: int) -> bool:
        """Вернуть True, если с последнего свидетельства прошло меньше disengage_timeout_s."""
        if self._last_evidence_ns is None:
            return False
        return (now_ns - self._last_evidence_ns) < int(self.disengage_timeout_s * 1e9)

    def seconds_since_evidence(self, *, now_ns: int) -> float:
        """`inf`, если свидетельств не было вообще (а не 0.0 -- это разные вещи)."""
        if self._last_evidence_ns is None:
            return float("inf")
        return max(0.0, (now_ns - self._last_evidence_ns) / 1e9)

    @property
    def last_source(self) -> str:
        """Источник последнего засчитанного свидетельства."""
        return self._last_source

    @property
    def last_evidence_ns(self) -> int | None:
        """Метка времени последнего засчитанного свидетельства, None -- если его не было."""
        return self._last_evidence_ns


def vad_evidence_allowed(
    *,
    ignore_vad_while_speaking: bool,
    speaking: bool,
    seconds_since_speaking_ended: float | None,
    tts_tail_s: float,
) -> bool:
    """Design §6: без AEC собственная речь триггерит VAD -- такое свидетельство надо отбрасывать.

    `seconds_since_speaking_ended` -- None, если робот ещё ни разу не
    говорил (в этом случае гейт не применим, VAD пропускается).
    """
    if not ignore_vad_while_speaking:
        return True
    if speaking:
        return False
    if seconds_since_speaking_ended is None:
        return True
    return seconds_since_speaking_ended >= tts_tail_s
