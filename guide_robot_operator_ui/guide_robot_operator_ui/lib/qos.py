"""Профили QoS входов operator_ui.

Единственный модуль в lib/, которому разрешено импортировать rclpy
(CLAUDE.md: "QoS profiles live only in lib/qos.py"). QOS_MISSION_STATE --
копия издателя, не импорт: guide_robot_mission_control/lib/qos.py:42-50,
несовпадение QoS между издателем и подписчиком не даёт ошибки -- оно молча
не даёт соединения.
"""

from __future__ import annotations

from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy

__all__ = ["QOS_MISSION_STATE"]

QOS_MISSION_STATE = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)
