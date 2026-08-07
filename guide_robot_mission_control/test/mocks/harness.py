"""Единый MultiThreadedExecutor для моков (+ опционально узла под тестом), design §9.1.

Один rclpy.Context на harness, не глобальный rclpy.init(): тесты этого
пакета живут в одном процессе pytest, повторный global init()/shutdown()
между тестами не переживёт второй тест в файле.

Все узлы поднимаются с use_sim_time:=true. Пейсинг в моках -- обычный
`time.sleep()` на реальных миллисекундах для поллинга, но решения о том,
закончилось ли "время говорения" или "время движения", принимаются по
self.get_clock().now(), которая читает /clock. Поэтому advance() у
SimClock -- это скачок вперёд, а не тиканье: подконтрольные потоки
обнаруживают его на следующем опросе (~1 мс реального времени), и тесту
не нужен фоновый тикающий таймер.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.task import Future

from test.mocks.mock_nav_server import MockNavServer
from test.mocks.mock_say_server import MockSayServer
from test.mocks.mock_semantic_map import (
    MockContentServer,
    MockLocationServer,
    MockRoutePlanner,
    SemanticMapFixtures,
)
from test.mocks.sim_clock import SimClock

__all__ = ["MissionTestHarness", "wait_for_future", "wait_until"]

_USE_SIM_TIME = [Parameter("use_sim_time", value=True)]


def wait_for_future(future: Future, timeout_s: float = 5.0) -> None:
    """Дождаться future из другого потока, не спиная executor повторно.

    Executor уже крутится в фоновом потоке harness-а; звать
    rclpy.spin_until_future_complete() отсюда означало бы второй спиннер
    на том же executor-е из другого потока -- гонка, а не защита от неё.
    """
    done = threading.Event()
    future.add_done_callback(lambda _f: done.set())
    if not done.wait(timeout_s):
        msg = f"future не завершился за {timeout_s} с"
        raise TimeoutError(msg)


def wait_until(
    predicate: Callable[[], bool], timeout_s: float = 5.0, period_s: float = 0.001
) -> None:
    """Опрашивать predicate() реальными миллисекундами, пока не станет True.

    Для синхронизации с эффектом асинхронной публикации: например, после
    `cancel_all_pub.publish(...)` нужно дождаться, чтобы подписка мока
    реально обработала сообщение (self.say.epoch увеличился), прежде чем
    двигать sim-время дальше -- иначе advance() может обогнать доставку
    сообщения, и пейсинг успеет завершиться до эпохального фенсинга.
    """
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            msg = f"условие не выполнилось за {timeout_s} с"
            raise TimeoutError(msg)
        time.sleep(period_s)


class MissionTestHarness:
    """Поднимает моки (и, по требованию, узлы под тестом) в одном executor-е."""

    def __init__(self, *, num_threads: int = 12) -> None:
        """Создать изолированный rclpy.Context, поднять моки, запустить спин в фоне."""
        self.context = rclpy.Context()
        rclpy.init(context=self.context)
        self.executor = MultiThreadedExecutor(context=self.context, num_threads=num_threads)

        self.clock = SimClock(context=self.context, parameter_overrides=_USE_SIM_TIME)
        self.fixtures = SemanticMapFixtures()
        self.say = MockSayServer(context=self.context, parameter_overrides=_USE_SIM_TIME)
        self.nav = MockNavServer(context=self.context, parameter_overrides=_USE_SIM_TIME)
        self.content_server = MockContentServer(
            self.fixtures, context=self.context, parameter_overrides=_USE_SIM_TIME
        )
        self.location_server = MockLocationServer(
            self.fixtures, context=self.context, parameter_overrides=_USE_SIM_TIME
        )
        self.route_planner = MockRoutePlanner(
            self.fixtures, context=self.context, parameter_overrides=_USE_SIM_TIME
        )
        self._owned_nodes = [
            self.clock,
            self.say,
            self.nav,
            self.content_server,
            self.location_server,
            self.route_planner,
        ]
        for node in self._owned_nodes:
            self.executor.add_node(node)

        self._extra_nodes: list[Node] = []
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        with contextlib.suppress(rclpy.executors.ExternalShutdownException):
            self.executor.spin()

    def add_node(self, node: Node) -> None:
        """Добавить узел под тестом (например, narration_server) в общий executor."""
        self.executor.add_node(node)
        self._extra_nodes.append(node)

    def make_client_node(self, name: str = "test_client") -> Node:
        """Создать вспомогательный узел-клиент (для ActionClient/ServiceClient в тесте)."""
        node = Node(name, context=self.context, parameter_overrides=_USE_SIM_TIME)
        self.add_node(node)
        return node

    def shutdown(self) -> None:
        """Остановить executor, дождаться потока спина, снять узлы, закрыть контекст.

        Порядок важен: destroy_node() рвёт handle-ы подписок/сервисов, а
        фоновый поток может в этот момент быть в середине spin_once() и
        держать один из них -- destroy до join() ловит
        `InvalidHandle: cannot use Destroyable because destruction was
        requested` (безобидно по сути, но шумит в выводе тестов).
        """
        self.executor.shutdown(timeout_sec=5.0)
        self._thread.join(timeout=5.0)
        for node in (*self._extra_nodes, *self._owned_nodes):
            node.destroy_node()
        rclpy.shutdown(context=self.context)
