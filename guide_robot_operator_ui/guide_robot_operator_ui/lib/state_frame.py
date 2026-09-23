"""Кадр состояния для WS operator_ui -- чистая функция, без rclpy (design C4).

`MissionState` -- сгенерированный тип сообщения, не rclpy-объект: импорт
здесь не нарушает правило "lib/ без rclpy" (CLAUDE.md), только модуль
`lib/qos.py` имеет дело с самим rclpy-рантаймом (QoSProfile).
"""

from __future__ import annotations

from typing import Any

from guide_robot_msgs.msg import MissionState

__all__ = ["STATE_NAMES", "build_frame"]

# Копия mission_fsm_node.py's _STATE_ENUM (имя -> код), плюс "idle" -- у
# mission_fsm нет причины именовать IDLE по имени (в него не переходят
# явно), а UI обязан его показывать как обычное состояние. Тест
# (test_state_frame.py) сверяет множество значений с STATE_*-константами
# MissionState.msg -- расхождение ловится сборкой, не глазами (design C4).
STATE_NAMES: dict[int, str] = {
    MissionState.STATE_IDLE: "idle",
    MissionState.STATE_GREETING: "greeting",
    MissionState.STATE_NAVIGATING: "navigating",
    MissionState.STATE_NARRATING: "narrating",
    MissionState.STATE_ANSWERING: "answering",
    MissionState.STATE_AWAITING_CONFIRM: "awaiting_confirm",
    MissionState.STATE_PAUSED: "paused",
    MissionState.STATE_HELD: "held",
    MissionState.STATE_RETURNING: "returning",
}

# Ни один MissionState.STATE_* не отрицателен -- безопасный "ничего ещё не
# приходило" (cold start, design C4), отличимый от любого реального кода.
_UNKNOWN_STATE = -1


def build_frame(
    *,
    seq: int,
    mission_msg: MissionState | None,
    estop: bool,
    supervisor_state: str,
    now_s: float,
    mission_received_at_s: float | None,
) -> dict[str, Any]:
    """Собрать один WS-кадр (design C4).

    `mission_received_at_s` -- момент (те же часы, что и `now_s`)
    последнего ПОЛУЧЕННОГО /mission/state, не штамп из самого сообщения --
    возраст обязан отражать реальную связь с mission_fsm, а не то, что
    узел сам туда пишет. `None` -- сообщение ещё ни разу не пришло: тогда
    `mission_state_age_s` -- JSON `null`, а не `0.0` -- "давно не было" и
    "никогда не было" не одно и то же для гейта сброса локализации (C5.2).
    """
    if mission_msg is None:
        state = _UNKNOWN_STATE
        state_name = "unknown"
        tour_id = ""
        stop_index = 0
        stop_total = 0
        stop_id = ""
        exhibit_id = ""
        next_exhibit_id = ""
        chunk_index = 0
        chunk_total = 0
        paused_reason = 0
    else:
        state = int(mission_msg.state)
        state_name = STATE_NAMES.get(state, "unknown")
        tour_id = mission_msg.tour_id
        stop_index = int(mission_msg.stop_index)
        stop_total = int(mission_msg.stop_total)
        stop_id = mission_msg.stop_id
        exhibit_id = mission_msg.exhibit_id
        next_exhibit_id = mission_msg.next_exhibit_id
        chunk_index = int(mission_msg.chunk_index)
        chunk_total = int(mission_msg.chunk_total)
        paused_reason = int(mission_msg.pause_reason)

    mission_state_age_s = (
        None if mission_received_at_s is None else max(0.0, now_s - mission_received_at_s)
    )

    return {
        "seq": seq,
        "stamp": now_s,
        "state": state,
        "state_name": state_name,
        "tour_id": tour_id,
        "stop_index": stop_index,
        "stop_total": stop_total,
        "stop_id": stop_id,
        "exhibit_id": exhibit_id,
        "next_exhibit_id": next_exhibit_id,
        "chunk_index": chunk_index,
        "chunk_total": chunk_total,
        "paused_reason": paused_reason,
        "estop": estop,
        "supervisor_state": supervisor_state,
        "mission_state_age_s": mission_state_age_s,
    }
