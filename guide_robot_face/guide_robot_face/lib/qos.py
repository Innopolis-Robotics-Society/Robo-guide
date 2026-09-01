"""Профили QoS топиков лица и его входов.

Единственный модуль в lib/, которому разрешено импортировать rclpy.
Значения скопированы с издателей (voice/llm/mission): несовпадение QoS
между издателем и подписчиком молча не даёт соединения.
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = [
    "QOS_DIALOG_PHASE",
    "QOS_FACE_STATE",
    "QOS_MISSION_PRESENCE",
    "QOS_MISSION_STATE",
    "QOS_VAD",
    "QOS_VOICE_SPEAKING",
    "QOS_WAKEWORD",
]

# /face/state -- TRANSIENT_LOCAL: поздно поднявшийся подписчик обязан
# сразу увидеть текущее выражение (то же рассуждение что у /mission/state).
QOS_FACE_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

QOS_DIALOG_PHASE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

QOS_MISSION_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

QOS_MISSION_PRESENCE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

QOS_VOICE_SPEAKING = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

QOS_VAD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)

QOS_WAKEWORD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
