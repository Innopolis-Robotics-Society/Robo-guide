"""Плоская геометрия на границе с ROS-сообщениями (design.md §0.5).

locations.yaml хранит ориентацию как yaw в радианах ("встать лицом к
экспонату"), а Location.msg.pose -- geometry_msgs/PoseStamped с
кватернионом. Функция возвращает голые float, а не geometry_msgs-типы,
чтобы оставаться тестируемой без rclpy -- узел сам раскладывает результат
по полям сообщения.
"""

from __future__ import annotations

import math

__all__ = ["yaw_to_quaternion_xyzw"]


def yaw_to_quaternion_xyzw(yaw: float) -> tuple[float, float, float, float]:
    """Кватернион поворота на yaw радиан вокруг оси Z. Пол и крен считаются нулевыми."""
    half = yaw / 2.0
    return (0.0, 0.0, math.sin(half), math.cos(half))
