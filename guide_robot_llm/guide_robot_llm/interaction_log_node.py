"""interaction_log -- jsonl-sink для ходов диалога (llm_plam.md §3/§6).

Третий и последний lifecycle-узел пакета, отдельный процесс от `dialog_agent`
(та же причина, что у `~/call_tool` на шаге 5): медленный диск/I/O здесь не
должен блокировать критический путь `dialog_agent` (barge-in abort,
tool-calling). Подписан на `/dialog/interaction`, публикуемый
`dialog_agent_node.py` fire-and-forget -- симметрично design-принципу
`tool_broker`: "остаётся рабочим, если [другая сторона] выключена" --
`dialog_agent` не должен зависеть от того, жив ли `interaction_log`.
"""

from __future__ import annotations

import json

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_llm.lib.interaction_sink import InteractionSink
from guide_robot_llm.lib.qos import QOS_INTERACTION_EVENT
from guide_robot_msgs.msg import InteractionEvent

__all__ = ["InteractionLogNode", "main"]


class InteractionLogNode(LifecycleNode):
    """Lifecycle-нода: слушает `/dialog/interaction`, пишет в jsonl через `InteractionSink`."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. `InteractionSink`/подписка -- в `on_configure`."""
        super().__init__("interaction_log", **node_kwargs)

        self.declare_parameter("log_dir", "~/.guide_robot/llm_turns")

        self._sink: InteractionSink | None = None
        self._seen_turn_keys: set[tuple[str, int]] = set()
        self._cb_reentrant = ReentrantCallbackGroup()

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Открыть sink-файл, поднять подписку."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        log_dir = str(self.get_parameter("log_dir").value)
        self._sink = InteractionSink(log_dir)

        self._event_sub = self.create_subscription(
            InteractionEvent,
            "/dialog/interaction",
            self._on_event,
            QOS_INTERACTION_EVENT,
            callback_group=self._cb_reentrant,
        )

        self.get_logger().info(f"interaction_log сконфигурирован: log_dir={self._sink.path}")
        return TransitionCallbackReturn.SUCCESS

    def _on_event(self, msg: InteractionEvent) -> None:
        try:
            record = json.loads(msg.payload_json)
        except json.JSONDecodeError as error:
            # Баг на стороне publisher-а (dialog_agent) не повод ронять sink
            # целиком -- остальные записи этой и следующих сессий не должны
            # от этого пострадать.
            self.get_logger().error(f"битый payload_json: {error}")
            return
        # Повтор (session_id, turn_id) -- улика второго живого dialog_agent
        # (или незакрытой старой подписки): пишем всё равно (улики не теряем),
        # но заметно предупреждаем.
        session_id = record.get("session_id")
        turn_id = record.get("turn_id")
        if isinstance(session_id, str) and isinstance(turn_id, int):
            key = (session_id, turn_id)
            if key in self._seen_turn_keys:
                self.get_logger().warning(
                    f"повтор записи session_id={session_id} turn_id={turn_id} -- "
                    "возможно, работает второй dialog_agent"
                )
            self._seen_turn_keys.add(key)
        if self._sink is not None:
            self._sink.write(record)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Закрыть sink между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None
        self._seen_turn_keys.clear()
        # Подписку уничтожаем явно: cleanup -> configure иначе оставляет ДВЕ
        # живые подписки на /dialog/interaction -- каждая запись писалась бы
        # дважды (живой баг: задвоенные turn_id в jsonl).
        if getattr(self, "_event_sub", None) is not None:
            self.destroy_subscription(self._event_sub)
            self._event_sub = None


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = InteractionLogNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
