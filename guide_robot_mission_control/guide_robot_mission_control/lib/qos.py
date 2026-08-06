"""Профили QoS для топиков, которые mission_control делит с voice/semantic_map.

Копия значений из guide_robot_voice/guide_robot_voice/lib/qos.py, не импорт
оттуда: пакет умышленно не зависит от guide_robot_voice (design §0.5 --
narration_server не чанкует и не использует TextChunker), а несовпадение
QoS между издателем и подписчиком не даёт ошибки -- оно молча не даёт
соединения. Держать эти профили одним файлом здесь же, чтобы не размазывать
их по узлам пакета.
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = [
    "QOS_ASR_TRANSCRIPT",
    "QOS_CANCEL_ALL",
    "QOS_MISSION_PRESENCE",
    "QOS_MISSION_STATE",
    "QOS_VAD",
    "QOS_VOICE_SPEAKING",
    "QOS_WAKEWORD",
]

# /speech/cancel_all -- команда безопасности, обязана дойти.
QOS_CANCEL_ALL = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /voice/speaking -- TRANSIENT_LOCAL: поздно поднявшийся подписчик обязан
# сразу узнать состояние, не дожидаясь следующего heartbeat.
QOS_VOICE_SPEAKING = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

# /mission/state -- design §7: RELIABLE, TRANSIENT_LOCAL, depth 1. Живёт
# здесь заранее (не используется в этом шаге) -- mission_fsm понадобится
# тот же профиль, лучше не дублировать объявление.
QOS_MISSION_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

# /speech/wakeword -- событие, редкое и обязанное дойти (см.
# guide_robot_voice/lib/qos.py -- то же значение).
QOS_WAKEWORD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /vad -- высокая частота, свежий кадр важнее пропущенного.
QOS_VAD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)

# /asr/transcript -- финалы редкие и обязаны дойти все.
QOS_ASR_TRANSCRIPT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /mission/presence -- design §6/§7: RELIABLE, TRANSIENT_LOCAL, depth 1,
# heartbeat 1 Гц. Поздно поднявшийся подписчик (например, mission_fsm)
# обязан сразу узнать текущее состояние присутствия.
QOS_MISSION_PRESENCE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)
