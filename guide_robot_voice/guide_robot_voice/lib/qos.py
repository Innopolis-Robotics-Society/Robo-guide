"""Профили QoS для всех топиков пакета, одним местом.

Единственный модуль в lib/, которому разрешено импортировать rclpy: его
профили нужны только на границе с ROS, а не в тестируемой логике. Держать
их здесь, а не размазывать по нодам, -- чтобы `/vad` или `/speech/wakeword`
не оказались RELIABLE в одной ноде и BEST_EFFORT в другой по опечатке:
несовпадение QoS между издателем и подписчиком не даёт ошибки, оно молча
не даёт соединения.

Значения -- из таблицы контракта пакета (§2 design-документа).
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = [
    "QOS_ASR_PARTIAL",
    "QOS_ASR_TRANSCRIPT",
    "QOS_AUDIO_MIC",
    "QOS_CANCEL_ALL",
    "QOS_SYSTEM_EVENT",
    "QOS_VAD",
    "QOS_VOICE_SPEAKING",
    "QOS_WAKEWORD",
]

# /audio/mic -- 62.5 Гц, потеря кадра не критична, накопление задержки хуже.
QOS_AUDIO_MIC = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=5,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)

# /vad -- 31.25 Гц, та же логика: свежий кадр важнее любого пропущенного.
QOS_VAD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)

# /speech/wakeword -- событие, редкое и обязанное дойти.
QOS_WAKEWORD = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /asr/partial -- 5-10 Гц, BEST_EFFORT: партиалы на RELIABLE забивают
# очередь и тормозят финалы (см. design §0, пункт 4).
QOS_ASR_PARTIAL = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)

# /asr/transcript -- финалы редкие и обязаны дойти все, отсюда глубина 10.
QOS_ASR_TRANSCRIPT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
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

# /speech/cancel_all -- команда безопасности, обязана дойти.
QOS_CANCEL_ALL = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)

# /system_event -- редкое, для диагностики, обязано дойти.
QOS_SYSTEM_EVENT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
