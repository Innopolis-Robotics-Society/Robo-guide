"""Мок nav2_msgs/action/NavigateToPose для тестов nav_client.py (design §9.1).

Реальный сервер этого действия -- bt_navigator из Nav2, не пакет этого
репозитория; полный Nav2-стек в CI не поднимается (design §9: "без железа
и без Gazebo"), поэтому mission тестируется против этого мока.

Скорость -- по self.get_clock(), узел обязан подниматься с
use_sim_time:=true и получать /clock от sim_clock.py.
"""

from __future__ import annotations

import time

import rclpy
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node

__all__ = ["MockNavServer"]

_POLL_S = 0.001


class MockNavServer(Node):
    """NavigateToPose на виртуальных часах с настраиваемым исходом."""

    MODE_SUCCEED = "succeed"
    MODE_ABORT = "abort"
    MODE_HANG = "hang"

    def __init__(self, node_name: str = "mock_nav_server", **node_kwargs: object) -> None:
        """Поднять action-сервер `navigate_to_pose`."""
        super().__init__(node_name, **node_kwargs)
        self.mode = self.MODE_SUCCEED
        self.distance_m = 5.0
        self.duration_s = 2.0
        # Режим следующего goal-а, если задан явно -- потребляется один раз
        # и снимается, иначе используется self.mode. Позволяет тесту менять
        # исход по ходу одного RunTour (например, второй перегон -- hang).
        self.mode_queue: list[str] = []

        self._action_server = ActionServer(
            self,
            NavigateToPose,
            "navigate_to_pose",
            execute_callback=self._execute,
            cancel_callback=lambda handle: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )

    def _ok(self) -> bool:
        """rclpy.ok() для СВОЕГО контекста, не глобального дефолтного.

        См. mock_say_server._ok(): голый rclpy.ok() под отдельным
        rclpy.Context() (harness.py) всегда False, а не "нода жива".
        """
        return rclpy.ok(context=self.context)

    def _execute(self, goal_handle: object) -> NavigateToPose.Result:
        request: NavigateToPose.Goal = goal_handle.request  # type: ignore[attr-defined]
        mode = self.mode_queue.pop(0) if self.mode_queue else self.mode
        start = self.get_clock().now()

        if mode == self.MODE_HANG:
            while self._ok():
                if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                    goal_handle.canceled()  # type: ignore[attr-defined]
                    return NavigateToPose.Result()
                time.sleep(_POLL_S)
            return NavigateToPose.Result()

        while True:
            elapsed = (self.get_clock().now() - start).nanoseconds / 1e9
            remaining_ratio = max(0.0, 1.0 - elapsed / self.duration_s)

            if goal_handle.is_cancel_requested:  # type: ignore[attr-defined]
                goal_handle.canceled()  # type: ignore[attr-defined]
                return NavigateToPose.Result()

            feedback = NavigateToPose.Feedback()
            feedback.current_pose = request.pose
            feedback.distance_remaining = self.distance_m * remaining_ratio
            goal_handle.publish_feedback(feedback)  # type: ignore[attr-defined]

            if elapsed >= self.duration_s:
                break
            time.sleep(_POLL_S)

        if mode == self.MODE_ABORT:
            goal_handle.abort()  # type: ignore[attr-defined]
        else:
            goal_handle.succeed()  # type: ignore[attr-defined]
        return NavigateToPose.Result()
