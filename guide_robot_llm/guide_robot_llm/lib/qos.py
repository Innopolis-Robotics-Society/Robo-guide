"""Профили QoS для топиков, которые guide_robot_llm делит с mission_control.

Копия значений из guide_robot_mission_control/guide_robot_mission_control/
lib/qos.py, не импорт: пакет умышленно не зависит от guide_robot_mission_control
в рантайме (та же причина, что у copy между voice/mission_control -- см.
докстринг соответствующего файла там), а несовпадение QoS между издателем
и подписчиком не даёт ошибки -- оно молча не даёт соединения.
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = [
    "QOS_ASR_TRANSCRIPT",
    "QOS_CANCEL_ALL",
    "QOS_INTERACTION_EVENT",
    "QOS_MISSION_PRESENCE",
    "QOS_MISSION_STATE",
]

# /mission/state -- design §7: RELIABLE, TRANSIENT_LOCAL, depth 1. Поздно
# поднявшийся tool_broker обязан сразу увидеть текущее состояние тура, не
# дожидаясь следующего heartbeat.
QOS_MISSION_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

# /mission/presence -- design §6/§7: то же самое соображение про поздний
# подписчик.
QOS_MISSION_PRESENCE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

# /speech/cancel_all -- команда безопасности/barge-in, обязана дойти
# (dialog_agent, llm_plam.md §6: abort in-flight HTTP-запроса по ней).
QOS_CANCEL_ALL = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /dialog/interaction -- события хода диалога (llm_plam.md §6), редкие
# (один-два за ход), но подряд идущие в конце хода не должны теряться --
# RELIABLE. VOLATILE: interaction_log -- офлайн-анализ, поздний подписчик
# не обязан видеть события, случившиеся до его запуска.
QOS_INTERACTION_EVENT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /asr/transcript -- финалы редкие и обязаны дойти все.
QOS_ASR_TRANSCRIPT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
