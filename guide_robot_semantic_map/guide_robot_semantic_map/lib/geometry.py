"""Плоская геометрия на границе с ROS-сообщениями (design.md §0.5, §1.2).

locations.yaml хранит ориентацию как yaw в радианах ("встать лицом к
экспонату"), а Location.msg.pose -- geometry_msgs/PoseStamped с
кватернионом; nav_msgs/Path из ComputeRoute -- плотная последовательность
поз, а distance_m (design.md §1.2) -- сумма евклидовых отрезков между
ними. Обе функции возвращают/принимают голые float/кортежи, а не
geometry_msgs-типы, чтобы оставаться тестируемыми без rclpy -- узел сам
раскладывает результат по полям сообщения.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["path_length_m", "yaw_to_quaternion_xyzw"]


def yaw_to_quaternion_xyzw(yaw: float) -> tuple[float, float, float, float]:
    """Кватернион поворота на yaw радиан вокруг оси Z. Пол и крен считаются нулевыми."""
    half = yaw / 2.0
    return (0.0, 0.0, math.sin(half), math.cos(half))


def path_length_m(points: Sequence[tuple[float, float]]) -> float:
    """Сумма евклидовых отрезков плотного пути (design.md §1.2: distance_m, не route_cost)."""
    total = 0.0
    for (x1, y1), (x2, y2) in pairwise(points):
        total += math.hypot(x2 - x1, y2 - y1)
    return total
