"""Маппинг сигналов пайплайна в имя состояния лица.

Чистая функция: без rclpy и без guide_robot_msgs (константы совпадают
с DialogPhase.msg / MissionState.msg). Affect (happy/…) здесь нет.
"""

from __future__ import annotations

from dataclasses import dataclass

# DialogPhase.msg
PHASE_IDLE = 0
PHASE_ACTION = 1
PHASE_ANSWER = 2
PHASE_AWAITING = 3

# MissionState.STATE_NAVIGATING / STATE_RETURNING -- оба «едем».
MISSION_NAVIGATING = 2
MISSION_RETURNING = 8

_PRIORITY = ("error", "driving", "speaking", "thinking", "listening")


@dataclass
class FaceInputs:
    """Снимок входов агрегатора. Поля -- уже сведённые bool/int, не ROS-msg."""

    supervisor_fault: bool = False
    speaking: bool = False
    dialog_phase: int = PHASE_IDLE
    navigating: bool = False
    vad_active: bool = False
    wakeword_hold: bool = False
    presence: bool = False


def decide(inp: FaceInputs) -> str:
    """Выбрать pipeline-состояние по жёсткому приоритету.

    error > driving > speaking > thinking > listening > idle|sleep.
    Езда бьёт thinking/speaking: иначе ACTION старта тура и транзитный
    TTS держат «думаю»/«говорю» всю дорогу до остановки.
    """
    listening = inp.vad_active or inp.wakeword_hold or inp.dialog_phase == PHASE_AWAITING
    flags = {
        "error": inp.supervisor_fault,
        "speaking": inp.speaking,
        "thinking": inp.dialog_phase in (PHASE_ACTION, PHASE_ANSWER),
        "driving": inp.navigating,
        "listening": listening,
    }
    for name in _PRIORITY:
        if flags[name]:
            return name
    return "idle" if inp.presence else "sleep"
