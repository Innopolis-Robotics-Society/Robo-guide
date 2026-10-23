"""nav_snapshot: rosbag навигационных топиков вокруг каждого эпизода recovery.

Держит в памяти последние `pre_s` секунд выбранных топиков (подписки raw=True:
сообщения не десериализуются, хранятся байтами CDR). Когда узел BT
`trigger_node` (RecoveryFallback в navigate_to_pose_with_recovery.xml)
переходит в RUNNING, пишет буфер и ещё `post_s` секунд в rosbag (sqlite3) в
`output_dir/<время>_<причина>/`, плюс сам /behavior_tree_log. Хранится не
больше `keep_last` снимков, старые удаляются.

Смотреть: `ros2 bag play` + RViz (scan, local_costmap, plan), или
`ros2 bag info` для состава. Логика окна -- snapshot_window.py.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import rclpy
import rosbag2_py
from nav2_msgs.msg import BehaviorTreeLog
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from rclpy.serialization import serialize_message
from rosidl_runtime_py.utilities import get_message

from guide_robot_navigation.snapshot_window import Record, Snapshot, SnapshotWindow

BT_LOG_TOPIC = "/behavior_tree_log"
BT_LOG_TYPE = "nav2_msgs/msg/BehaviorTreeLog"

DEFAULT_TOPICS = [
    "/scan:sensor_msgs/msg/LaserScan",
    "/scan_left:sensor_msgs/msg/LaserScan",
    "/scan_right:sensor_msgs/msg/LaserScan",
    "/local_costmap/costmap:nav_msgs/msg/OccupancyGrid",
    "/plan:nav_msgs/msg/Path",
    "/amcl_pose:geometry_msgs/msg/PoseWithCovarianceStamped",
    "/cmd_vel_mux_safety:geometry_msgs/msg/Twist",
    "/cmd_vel_filtered:geometry_msgs/msg/Twist",
    "/sonar/range/sonar_sensor_1:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_2:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_4:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_5:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_6:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_8:sensor_msgs/msg/Range",
    "/sonar/range/sonar_sensor_9:sensor_msgs/msg/Range",
]


class NavSnapshotNode(Node):
    """Буферизует навигационные топики и пишет снимок при входе BT в recovery."""

    def __init__(self) -> None:
        """Объявить параметры, подписаться на топики и /behavior_tree_log."""
        super().__init__("nav_snapshot")
        self.declare_parameter("topics", DEFAULT_TOPICS)
        self.declare_parameter("trigger_node", "RecoveryFallback")
        self.declare_parameter("pre_s", 15.0)
        self.declare_parameter("post_s", 30.0)
        self.declare_parameter("max_s", 120.0)
        self.declare_parameter("output_dir", "~/ros2_ws/log/nav_snapshots")
        self.declare_parameter("keep_last", 30)

        self._trigger_node = str(self.get_parameter("trigger_node").value)
        self._output_dir = os.path.expanduser(str(self.get_parameter("output_dir").value))
        self._keep_last = int(self.get_parameter("keep_last").value)
        self._window = SnapshotWindow(
            pre_s=float(self.get_parameter("pre_s").value),
            post_s=float(self.get_parameter("post_s").value),
            max_s=float(self.get_parameter("max_s").value),
        )
        # Колбэки и таймер -- в одном (по умолчанию однопоточном) исполнителе,
        # окно трогает только он. Поток записи получает готовый Snapshot.
        self._topic_types: dict[str, str] = {BT_LOG_TOPIC: BT_LOG_TYPE}
        for spec in self.get_parameter("topics").value:
            topic, _, type_name = str(spec).partition(":")
            if not type_name:
                self.get_logger().error(f"topics: {spec!r} без типа (нужно topic:pkg/msg/Type)")
                continue
            self._topic_types[topic] = type_name
            self.create_subscription(
                get_message(type_name),
                topic,
                lambda data, topic=topic: self._on_raw(topic, data),
                qos_profile_sensor_data,
                raw=True,
            )
        # BT-лог разбираем (нужен для триггера) и пишем в снимок как есть.
        # RELIABLE: потерянный переход в RUNNING -- потерянный снимок.
        self.create_subscription(
            BehaviorTreeLog,
            BT_LOG_TOPIC,
            self._on_bt_log,
            QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE),
        )
        self.create_timer(0.5, self._on_timer)
        self.get_logger().info(
            f"nav_snapshot: {len(self._topic_types) - 1} топиков, триггер "
            f"{self._trigger_node}->RUNNING, снимки в {self._output_dir}"
        )

    def _now_ns(self) -> int:
        return self.get_clock().now().nanoseconds

    def _on_raw(self, topic: str, data: bytes) -> None:
        self._window.add(Record(topic=topic, t_ns=self._now_ns(), data=data))

    def _on_bt_log(self, msg: BehaviorTreeLog) -> None:
        now_ns = self._now_ns()
        self._window.add(Record(topic=BT_LOG_TOPIC, t_ns=now_ns, data=serialize_message(msg)))
        for event in msg.event_log:
            if event.node_name == self._trigger_node and event.current_status == "RUNNING":
                if self._window.trigger(now_ns=now_ns, reason="recovery"):
                    self.get_logger().warning(
                        f"recovery: пишу снимок навигации (до {self._window.post_s:.0f} с после)"
                    )

    def _on_timer(self) -> None:
        snapshot = self._window.poll(now_ns=self._now_ns())
        if snapshot is not None:
            threading.Thread(target=self._write, args=(snapshot,), daemon=True).start()

    def _write(self, snapshot: Snapshot) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        uri = os.path.join(self._output_dir, f"{stamp}_{snapshot.reason}")
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            writer = rosbag2_py.SequentialWriter()
            writer.open(
                rosbag2_py.StorageOptions(uri=uri, storage_id="sqlite3"),
                rosbag2_py.ConverterOptions("cdr", "cdr"),
            )
            for topic in sorted({r.topic for r in snapshot.records}):
                writer.create_topic(
                    rosbag2_py.TopicMetadata(
                        name=topic, type=self._topic_types[topic], serialization_format="cdr"
                    )
                )
            for record in snapshot.records:
                writer.write(record.topic, record.data, record.t_ns)
            del writer  # закрывает базу и пишет metadata.yaml
        except Exception as error:  # запись снимка не должна ронять узел
            self.get_logger().error(f"снимок {uri} не записан: {error}")
            return
        self.get_logger().info(
            f"снимок {uri}: {len(snapshot.records)} сообщений, "
            f"{(snapshot.end_ns - snapshot.trigger_ns) / 1e9:.0f} с после триггера, "
            f"recovery-входов {snapshot.triggers}"
        )
        self._prune()

    def _prune(self) -> None:
        try:
            entries = sorted(
                os.path.join(self._output_dir, d)
                for d in os.listdir(self._output_dir)
                if os.path.isdir(os.path.join(self._output_dir, d))
            )
        except OSError:
            return
        for stale in entries[: max(0, len(entries) - self._keep_last)]:
            shutil.rmtree(stale, ignore_errors=True)


def main(args: list[str] | None = None) -> None:
    """Запустить nav_snapshot до прерывания."""
    rclpy.init(args=args)
    node = NavSnapshotNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
