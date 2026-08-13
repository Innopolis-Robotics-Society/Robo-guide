"""Компактный снимок mission/presence для промпта ЛЛМ (DIALOG_REWORK_PLAN.md §3.4).

Чистая логика без rclpy -- `dialog_agent_node.py` раскладывает ROS-msg
(`MissionState`/`Presence`) по полям Protocol ниже, сама сборка dict-а
тестируется на голых dataclass-моках без ROS (design-конвенция пакета,
как `guide_robot_mission_control/presence.py`).

`safety`/`supervisor_state` по-прежнему не собираются: `estop` сейчас
читает только сам `mission_fsm` (не публикует наружу) -- вне рамок этого
шага. `told_ids`/`location_name`/`nearby` считает вызывающий код
(`dialog_agent_node.py`) -- этот модуль только раскладывает уже готовые
значения по ключам снимка, не решает, что в них должно быть.

`MissionState.tour_id`/`base_state` теперь заполняются
`mission_fsm_node._on_fsm_state_changed` (DIALOG_REWORK_PLAN.md §2.1) --
здесь они по-прежнему просто читаются как есть.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

__all__ = ["MissionStateLike", "PresenceLike", "build_snapshot", "render_status_line"]

_STATE_NAMES = {
    0: "IDLE",
    1: "GREETING",
    2: "NAVIGATING",
    3: "NARRATING",
    4: "ANSWERING",
    5: "AWAITING_CONFIRM",
    6: "PAUSED",
    7: "HELD",
    8: "RETURNING",
}
_IRQ_NONE = 0
_IRQ_NAMES = {0: "none", 1: "answer", 2: "confirm"}


class MissionStateLike(Protocol):
    """Поля `guide_robot_msgs/msg/MissionState`, которые нужны снимку."""

    state: int
    interrupt: int
    base_state: int
    tour_id: str
    stop_index: int
    stop_total: int
    stop_id: str
    resume_available: bool


class PresenceLike(Protocol):
    """Поля `guide_robot_msgs/msg/Presence`, которые нужны снимку."""

    present: bool
    seconds_since_evidence: float


def build_snapshot(
    mission: MissionStateLike,
    presence: PresenceLike,
    *,
    tools_allowed: list[str],
    location_zone: str = "",
    location_name: str = "",
    told_ids: Sequence[str] = (),
    nearby: Sequence[str] = (),
) -> dict:
    """Собрать компактный dict для промпта -- форма как в DIALOG_REWORK_PLAN.md §3.4.

    `tools_allowed` считает вызывающий код (`tools/schema.py`) по той же
    таблице гейтов, которой `tool_broker` пользуется для реального dispatch --
    снимок только отражает уже принятое решение, не принимает его сам.
    Из снимка ничего не убрано против предыдущей формы -- все новые ключи
    опциональны и появляются только когда вызывающий код их передал.
    """
    mission_section: dict[str, object] = {"state": _STATE_NAMES.get(mission.state, "UNKNOWN")}
    if mission.tour_id:
        mission_section["tour"] = mission.tour_id
    if mission.stop_total:
        mission_section["stop"] = mission.stop_index + 1  # человеку/ЛЛМ удобнее с 1
        mission_section["of"] = mission.stop_total
    if mission.stop_id:
        mission_section["location"] = mission.stop_id
    if location_name:
        mission_section["location_name"] = location_name
    if location_zone:
        mission_section["zone"] = location_zone
    if mission.interrupt != _IRQ_NONE:
        mission_section["interrupt"] = {
            "kind": _IRQ_NAMES.get(mission.interrupt, "unknown"),
            "base": _STATE_NAMES.get(mission.base_state, "IDLE"),
        }

    snap: dict[str, object] = {
        "mission": mission_section,
        "presence": {
            "present": bool(presence.present),
            "last_evidence_s": round(float(presence.seconds_since_evidence), 1),
        },
        "tools_allowed": list(tools_allowed),
    }
    if told_ids:
        snap["already_told"] = list(told_ids)
    if nearby:
        snap["nearby"] = list(nearby)
    return snap


def render_status_line(snap: dict) -> str:
    """Однострочный статус `[состояние: ...]` для последнего user-сообщения хода.

    Формат согласован с преамбулой системного промпта («Строка в квадратных
    скобках вида [состояние: ...] — служебное описание...») -- менять его
    можно только вместе с `config/system_prompt.txt`. В строку НАМЕРЕННО не
    идут `tools_allowed` (гейт уже зашит в GBNF-грамматику фазы действия) и
    `already_told` (шум для 8B-модели) -- в снимке-словаре для
    `interaction_log` они остаются.
    """
    mission = snap.get("mission", {})
    parts: list[str] = [f"состояние: {mission.get('state', 'UNKNOWN')}"]

    if mission.get("tour"):
        parts.append(f"тур \"{mission['tour']}\"")
    location = str(mission.get("location_name") or mission.get("location") or "")
    if mission.get("stop") and mission.get("of"):
        stop_part = f"остановка {mission['stop']} из {mission['of']}"
        if location:
            stop_part += f" — {location}"
        parts.append(stop_part)
    elif location:
        parts.append(f"у локации {location}")

    interrupt = mission.get("interrupt")
    if isinstance(interrupt, dict):
        parts.append(f"рассказ прерван ({interrupt.get('kind', 'unknown')})")

    presence = snap.get("presence", {})
    if presence.get("present"):
        parts.append("посетитель рядом")
    else:
        parts.append(f"посетителя не видно {presence.get('last_evidence_s', 0)} с")

    return "[" + ", ".join(parts) + "]"
