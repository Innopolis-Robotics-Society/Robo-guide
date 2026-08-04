"""Профили QoS для топиков, которые слушает `chat_node`, одним местом.

Единственный модуль в lib/, которому разрешено импортировать rclpy -- как
и в `guide_robot_voice.lib.qos`, откуда эти профили в первую очередь и
берутся: несовпадение QoS между издателем и подписчиком не даёт ошибки,
оно молча не даёт соединения, поэтому дублировать значения "на глаз"
нельзя. Если `guide_robot_voice` доступен как зависимость (обычный случай
на роботе и в CI, где оба пакета собраны рядом) -- реэкспортируем прямо
оттуда. Если нет (например, `guide_robot_llm` тестируется в изоляции) --
локальная копия тех же значений, взятых из контракта пакета (design §2).
"""

from __future__ import annotations

try:
    from guide_robot_voice.lib.qos import (
        QOS_ASR_TRANSCRIPT,
        QOS_CANCEL_ALL,
        QOS_SYSTEM_EVENT,
        QOS_VOICE_SPEAKING,
        QOS_WAKEWORD,
    )
except ImportError:
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

    # /asr/transcript -- финалы редкие и обязаны дойти все, отсюда глубина 10.
    QOS_ASR_TRANSCRIPT = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )

    # /speech/wakeword -- событие, редкое и обязанное дойти.
    QOS_WAKEWORD = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )

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

    # /system_event -- редкое, для диагностики, обязано дойти.
    QOS_SYSTEM_EVENT = QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )

__all__ = [
    "QOS_ASR_TRANSCRIPT",
    "QOS_CANCEL_ALL",
    "QOS_SYSTEM_EVENT",
    "QOS_VOICE_SPEAKING",
    "QOS_WAKEWORD",
]
