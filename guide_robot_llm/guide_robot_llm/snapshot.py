"""Компактный снимок mission/presence для промпта ЛЛМ (llm_plam.md §5).

Чистая логика без rclpy -- `tool_broker_node.py` раскладывает ROS-msg
(`MissionState`/`Presence`) по полям Protocol ниже, сама сборка dict-а
тестируется на голых dataclass-моках без ROS (design-конвенция пакета,
как `guide_robot_mission_control/presence.py`).

Полный снимок из §5 плана несёт ещё `safety`/`nearby` -- этот шаг их не
собирает: `estop`/`supervisor_state` сейчас читает только сам
`mission_fsm` (не публикует наружу), а `nearby` требует пересечения
`stop_id` с координатами локаций (`~/list_locations`), которое появится
вместе с `tool_broker`'ом в шаге 2. `location_zone` -- единственное
обогащение снаружи, которое уже можно подать (вызывающий код сам решает,
откуда его взять).

Отдельно: `MissionState.tour_id`/`base_state` сейчас не заполняются
`mission_fsm_node._on_fsm_state_changed` (см. код -- присваивает не все
поля сообщения), это существующий пробел выше по стеку, не этого модуля;
здесь они просто читаются как есть.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["MissionStateLike", "PresenceLike", "build_snapshot"]

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
) -> dict:
    """Собрать компактный dict для промпта -- форма как в llm_plam.md §5.

    `tools_allowed` считает вызывающий код (`tools/schema.py` в шаге 2) по
    той же таблице гейтов, которой `tool_broker` пользуется для реального
    dispatch -- снимок только отражает уже принятое решение, не принимает
    его сам.
    """
    mission_section: dict[str, object] = {"state": _STATE_NAMES.get(mission.state, "UNKNOWN")}
    if mission.tour_id:
        mission_section["tour"] = mission.tour_id
    if mission.stop_total:
        mission_section["stop"] = mission.stop_index + 1  # человеку/ЛЛМ удобнее с 1
        mission_section["of"] = mission.stop_total
    if mission.stop_id:
        mission_section["location"] = mission.stop_id
    if location_zone:
        mission_section["zone"] = location_zone
    if mission.interrupt != _IRQ_NONE:
        mission_section["interrupt"] = {
            "kind": _IRQ_NAMES.get(mission.interrupt, "unknown"),
            "base": _STATE_NAMES.get(mission.base_state, "IDLE"),
        }

    return {
        "mission": mission_section,
        "presence": {
            "present": bool(presence.present),
            "last_evidence_s": round(float(presence.seconds_since_evidence), 1),
        },
        "tools_allowed": list(tools_allowed),
    }
