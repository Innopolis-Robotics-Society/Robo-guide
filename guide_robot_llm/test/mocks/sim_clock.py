"""Публикатор /clock для тестов на use_sim_time.

Копия guide_robot_mission_control/test/mocks/sim_clock.py -- `test/` не
инсталлируется colcon-ом (`find_packages(exclude=["test"])`), а оба пакета
называют свой тестовый каталог `test`, так что кросс-пакетный импорт
`test.mocks.*` не работает: побеждает тот, что раньше на sys.path.
"""

from __future__ import annotations

from rclpy.node import Node
from rosgraph_msgs.msg import Clock

__all__ = ["SimClock"]


class SimClock(Node):
    """Владелец симулируемого времени; публикует /clock по явному вызову."""

    def __init__(
        self, node_name: str = "sim_clock", start_seconds: float = 0.0, **node_kwargs: object
    ) -> None:
        """Поднять паблишер /clock и сразу опубликовать start_seconds."""
        super().__init__(node_name, **node_kwargs)
        self._ns = int(start_seconds * 1e9)
        self._pub = self.create_publisher(Clock, "/clock", 10)
        self._publish()

    def _publish(self) -> None:
        msg = Clock()
        msg.clock.sec = int(self._ns // 1_000_000_000)
        msg.clock.nanosec = int(self._ns % 1_000_000_000)
        self._pub.publish(msg)

    def advance(self, seconds: float) -> None:
        """Продвинуть симулируемое время на seconds и опубликовать /clock."""
        self._ns += int(seconds * 1e9)
        self._publish()

    def set_seconds(self, seconds: float) -> None:
        """Установить абсолютное симулируемое время и опубликовать /clock."""
        self._ns = int(seconds * 1e9)
        self._publish()

    @property
    def seconds(self) -> float:
        """Текущее симулируемое время в секундах."""
        return self._ns / 1e9
