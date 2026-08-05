"""Профили QoS для топиков этого пакета.

Единственное исключение из "lib/ без rclpy" (design.md §3): профиль нужен
только на границе с ROS, а не в тестируемой логике, и лучше жить одним
местом, чем размазаться по нодам, где /system_event рискует оказаться
RELIABLE в одной и BEST_EFFORT в другой по опечатке.

/system_event -- общий топик диагностики guide_robot_* (см. также
guide_robot_voice/lib/qos.py, guide_robot_llm/lib/qos.py); профиль здесь
намеренно идентичен им.
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = ["QOS_SYSTEM_EVENT"]

# /system_event -- редкое, для диагностики, обязано дойти.
QOS_SYSTEM_EVENT = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.VOLATILE,
)
