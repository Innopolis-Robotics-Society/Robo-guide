"""Единый MultiThreadedExecutor для тестов tool_broker поверх реального mission_control-стека.

По образцу guide_robot_mission_control/test/mocks/harness.py (см. докстринг
sim_clock.py про причину копии, не импорта): один rclpy.Context на harness,
все узлы -- use_sim_time:=true, время двигается только явным advance().

В отличие от mission_control-овского харнесса, здесь дополнительно
поднимаются РЕАЛЬНЫЕ `MissionFsmNode`/`NarrationServerNode`
(guide_robot_mission_control -- уже установленный Python-пакет, импортable
как обычная библиотека, в отличие от его `test/`) -- llm_plam.md §2/§8:
"полный тур со сценарием прерывания... проходит через broker" требует
настоящего FSM, не ещё одного мока поверх мока.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import rclpy
from guide_robot_llm.dialog_agent_node import DialogAgentNode
from guide_robot_llm.interaction_log_node import InteractionLogNode
from guide_robot_llm.tool_broker_node import ToolBrokerNode
from guide_robot_mission_control.mission_fsm_node import MissionFsmNode
from guide_robot_mission_control.narration_server_node import NarrationServerNode
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.task import Future

from test.mocks.mock_llm_server import MockLlmServer
from test.mocks.mock_nav_server import MockNavServer
from test.mocks.mock_say_server import MockSayServer
from test.mocks.mock_semantic_map import (
    MockContentServer,
    MockLocationServer,
    MockRoutePlanner,
    SemanticMapFixtures,
)
from test.mocks.sim_clock import SimClock

__all__ = ["ToolBrokerTestHarness", "wait_for_future", "wait_until"]

_USE_SIM_TIME = [Parameter("use_sim_time", value=True)]


def wait_for_future(future: Future, timeout_s: float = 5.0) -> None:
    """Дождаться future из другого потока, не спиная executor повторно."""
    done = threading.Event()
    future.add_done_callback(lambda _f: done.set())
    if not done.wait(timeout_s):
        msg = f"future не завершился за {timeout_s} с"
        raise TimeoutError(msg)


def wait_until(
    predicate: Callable[[], bool], timeout_s: float = 5.0, period_s: float = 0.001
) -> None:
    """Опрашивать predicate() реальными миллисекундами, пока не станет True."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            msg = f"условие не выполнилось за {timeout_s} с"
            raise TimeoutError(msg)
        time.sleep(period_s)


def pump_clock(
    clock: SimClock, predicate: Callable[[], bool], *, step: float, max_iterations: int = 200
) -> None:
    """Прыгать sim-часами шагом `step`, пока predicate() не станет True."""
    for _ in range(max_iterations):
        if predicate():
            return
        clock.advance(step)
        time.sleep(0.02)
    wait_until(predicate, timeout_s=15.0)


class ToolBrokerTestHarness:
    """Поднимает mission_fsm + narration_server + tool_broker + моки в одном executor-е."""

    def __init__(self, *, num_threads: int = 16) -> None:
        """Создать изолированный rclpy.Context, поднять весь стек, запустить спин в фоне."""
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

        self.narration = NarrationServerNode(
            context=self.context,
            parameter_overrides=[
                *_USE_SIM_TIME,
                Parameter("lookahead", value=0),
                Parameter("resume_bridge_enabled", value=False),
                Parameter("hard_stop_result_timeout_s", value=1.0),
            ],
        )
        self.fsm = MissionFsmNode(
            context=self.context,
            parameter_overrides=[
                *_USE_SIM_TIME,
                Parameter("nav_stop_timeout_s", value=5.0),
                Parameter("confirm_timeout_s", value=3.0),
            ],
        )
        self.broker = ToolBrokerNode(context=self.context, parameter_overrides=_USE_SIM_TIME)

        # dialog_agent (llm_plam.md §5) -- отдельный процесс от tool_broker в
        # проде, здесь в одном контексте ради простоты теста; общается с ним
        # только через ~/call_tool, как и в проде. llm_server -- мок из шага 4
        # (test/mocks/mock_llm_server.py), не настоящий llama.cpp.
        self.llm_server = MockLlmServer()
        self.llm_server.start()
        self._system_prompt_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        self._system_prompt_file.write("Тестовый системный промпт.")
        self._system_prompt_file.close()
        self.dialog_agent = DialogAgentNode(
            context=self.context,
            parameter_overrides=[
                *_USE_SIM_TIME,
                Parameter("llm.base_urls", value=[self.llm_server.url]),
                Parameter("llm.connect_timeout_s", value=1.0),
                Parameter("llm.read_timeout_s", value=5.0),
                Parameter("system_prompt_path", value=self._system_prompt_file.name),
            ],
        )

        # interaction_log (llm_plam.md §6) -- третий процесс в проде, здесь
        # в одном контексте по той же причине, что dialog_agent выше.
        self._log_dir = tempfile.mkdtemp(prefix="guide_robot_llm_interaction_")
        self.interaction_log = InteractionLogNode(
            context=self.context,
            parameter_overrides=[*_USE_SIM_TIME, Parameter("log_dir", value=self._log_dir)],
        )

        self._owned_nodes = [
            self.clock,
            self.say,
            self.nav,
            self.content_server,
            self.location_server,
            self.route_planner,
            self.narration,
            self.fsm,
            self.broker,
            self.dialog_agent,
            self.interaction_log,
        ]
        for node in self._owned_nodes:
            self.executor.add_node(node)

        self._extra_nodes: list[Node] = []
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

        for node in (
            self.narration,
            self.fsm,
            self.broker,
            self.dialog_agent,
            self.interaction_log,
        ):
            assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
            assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS

    def _spin(self) -> None:
        with contextlib.suppress(rclpy.executors.ExternalShutdownException):
            self.executor.spin()

    def add_node(self, node: Node) -> None:
        """Добавить дополнительный узел (например, тестовый клиент) в общий executor."""
        self.executor.add_node(node)
        self._extra_nodes.append(node)

    def make_client_node(self, name: str = "test_client") -> Node:
        """Создать вспомогательный узел-клиент (для публикации /asr/transcript и т.п.)."""
        node = Node(name, context=self.context, parameter_overrides=_USE_SIM_TIME)
        self.add_node(node)
        return node

    def shutdown(self) -> None:
        """Остановить executor, дождаться потока спина, снять узлы, закрыть контекст."""
        self.executor.shutdown(timeout_sec=5.0)
        self._thread.join(timeout=5.0)
        for node in (*self._extra_nodes, *self._owned_nodes):
            node.destroy_node()
        rclpy.shutdown(context=self.context)
        self.llm_server.stop()
        Path(self._system_prompt_file.name).unlink(missing_ok=True)
        shutil.rmtree(self._log_dir, ignore_errors=True)
