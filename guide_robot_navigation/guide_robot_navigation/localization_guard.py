"""ROS localization monitor and independent fail-closed final velocity gate."""

import json
import math
import os
import time
from pathlib import Path

import rclpy
from action_msgs.msg import GoalStatusArray
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from lifecycle_msgs.srv import GetState
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy
from rclpy.time import Time
from std_msgs.msg import String
from std_srvs.srv import Empty, SetBool, Trigger
from tf2_ros import Buffer, TransformListener, TransformException
from guide_robot_msgs.action import Say
from guide_robot_navigation.localization_policy import LocalizationPolicy

QOS = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)


class LocalizationGuard(Node):
    def __init__(self, **kwargs):
        super().__init__("localization_guard", **kwargs)
        defaults = dict(
            position_limit=0.5,
            yaw_limit_deg=20.0,
            recovery_position=0.35,
            recovery_yaw_deg=15.0,
            frame="map",
            base_frame="base_link",
            latch_file="~/ros2_ws/log/localization-lost",
            help_text="Я потерялся и остановился. Пожалуйста, позовите оператора, "
            "чтобы он указал моё положение на карте.",
        )
        for key, value in defaults.items():
            self.declare_parameter(key, value)
        get = lambda key: self.get_parameter(key).value
        self.path = Path(get("latch_file")).expanduser()
        self.running_path = self.path.with_suffix(".running")
        unclean_exit = self.running_path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.running_path.write_text("monitor_running\n")
        self.policy = LocalizationPolicy(
            latched=self.path.exists() or unclean_exit,
            position_limit=get("position_limit"),
            yaw_limit=math.radians(get("yaw_limit_deg")),
            recovery_position=get("recovery_position"),
            recovery_yaw=math.radians(get("recovery_yaw_deg")),
        )
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self)
        self.pub = self.create_publisher(String, "/localization/status", QOS)
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self.sample, 10)
        self.create_subscription(Twist, "/cmd_vel_localization_input", self.velocity, 10)
        self.create_subscription(PoseWithCovarianceStamped, "/initialpose", self.initial, 10)
        self.navigation_active = False
        self.create_subscription(
            GoalStatusArray, "/navigate_to_pose/_action/status", self.nav_status, QOS
        )
        self.create_service(Trigger, "/localization/prepare_reset", self.prepare)
        self.create_service(SetBool, "/localization/costmaps_cleared", self.cleared)
        self.update = self.create_client(Empty, "/request_nomotion_update")
        self.active = self.create_client(GetState, "/amcl/get_state")
        self.say = ActionClient(self, Say, "/speech/say")
        self.moving_until = self.active_at = self.next_request = 0.0
        self.active_ok = False
        self.pending_update = self.pending_active = None
        self.update_started = self.active_started = 0.0
        self.prepared_until = 0.0
        self.reset_stamp = -1.0
        self.last_stamp = -1.0
        self.announced = self.policy.latched
        self.persisted = self.path.exists()
        self.create_timer(0.1, self.tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def destroy_node(self):
        if self.policy.state == "ready" and time.monotonic() >= self.moving_until:
            self.running_path.unlink(missing_ok=True)
        super().destroy_node()

    def tf_ok(self):
        try:
            t = self.buffer.lookup_transform(
                self.get_parameter("frame").value, self.get_parameter("base_frame").value, Time()
            )
            age = (self.get_clock().now() - Time.from_msg(t.header.stamp)).nanoseconds / 1e9
            return 0 <= age <= 3
        except TransformException:
            return False

    def velocity(self, msg):
        if any(abs(v) > 1e-5 for v in (msg.linear.x, msg.linear.y, msg.angular.z)):
            self.moving_until = time.monotonic() + 0.5

    def nav_status(self, msg):
        self.navigation_active = any(s.status in (1, 2, 3) for s in msg.status_list)

    def prepare(self, request, response):
        if self.navigation_active:
            response.success = False
            response.message = "navigation_still_stopping"
            return response
        self.prepared_until = time.monotonic() + 3
        # Block before publication of initialpose, even when previously ready.
        self.policy.reset(time.monotonic())
        self.policy.latched = True
        self.tick()
        response.success = True
        return response

    def initial(self, msg):
        if time.monotonic() > self.prepared_until:
            return
        self.prepared_until = 0
        self.reset_stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        self.policy.reset(time.monotonic())

    def cleared(self, request, response):
        self.policy.costmaps_cleared = bool(request.data)
        response.success = self.policy.state == "recovering"
        if not request.data:
            self.policy.lose("costmap_clear_failed")
        return response

    def sample(self, msg):
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        if msg.header.frame_id != self.get_parameter("frame").value or not 0 <= age <= 3:
            return
        if stamp <= self.last_stamp or (
            self.policy.state == "recovering" and stamp <= self.reset_stamp
        ):
            return
        self.last_stamp = stamp
        self.policy.sample(
            msg.pose.covariance, time.monotonic(), tf_ok=self.tf_ok() and self.active_ok
        )

    def tick(self):
        now = time.monotonic()
        for name in ("update", "active"):
            future = getattr(self, "pending_" + name)
            if (
                future is not None
                and not future.done()
                and now - getattr(self, name + "_started") > 2
            ):
                future.cancel()
                setattr(self, "pending_" + name, None)
        if self.pending_active is not None and self.pending_active.done():
            try:
                self.active_ok = self.pending_active.result().current_state.id == 3
                self.active_at = now
            except Exception:
                self.active_ok = False
            self.pending_active = None
        self.policy.tick(
            now,
            moving=now < self.moving_until,
            tf_ok=self.tf_ok(),
            amcl_active=(
                self.active_ok
                and now - self.active_at < 3
                and not (self.policy.state == "recovering" and self.navigation_active)
            ),
        )
        if now >= self.next_request:
            self.next_request = now + 0.5
            if self.active.service_is_ready() and self.pending_active is None:
                self.active_started = now
                self.pending_active = self.active.call_async(GetState.Request())
            if self.update.service_is_ready() and (
                self.pending_update is None or self.pending_update.done()
            ):
                self.update_started = now
                self.pending_update = self.update.call_async(Empty.Request())
        if self.policy.latched != self.persisted:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.policy.latched:
                    temp = self.path.with_suffix(".tmp")
                    with temp.open("w") as file:
                        file.write("operator_reset_required\n")
                        file.flush()
                        os.fsync(file.fileno())
                    temp.replace(self.path)
                else:
                    self.path.unlink(missing_ok=True)
                self.persisted = self.policy.latched
            except OSError:
                self.policy.lose("latch_storage_error")
                self.get_logger().error("Cannot persist localization interlock")
        state = self.policy.snapshot(now)
        state["heartbeat_monotonic"] = now
        # Gate requires a recent AMCL estimate even on the first command after a stop.
        state["motion_blocked"] |= not state["fresh"]
        self.pub.publish(String(data=json.dumps(state)))
        if self.policy.state == "lost" and not self.announced:
            self.announced = True
            if self.say.server_is_ready():
                goal = Say.Goal(
                    text=self.get_parameter("help_text").value,
                    priority=Say.Goal.PRIORITY_SAFETY,
                    scope=Say.Goal.SCOPE_SAFETY,
                    interruptible=False,
                    max_duration=20.0,
                )
                self.say.send_goal_async(goal).add_done_callback(self.said)
            else:
                self.get_logger().error("Localization help speech unavailable")
        if self.policy.state == "ready" and not self.policy.latched:
            self.announced = False

    def said(self, future):
        try:
            handle = future.result()
            if not handle.accepted:
                raise RuntimeError("rejected")
            handle.get_result_async().add_done_callback(self.speech_result)
        except Exception:
            self.get_logger().error("Localization help speech rejected")

    def speech_result(self, future):
        try:
            if future.result().result.status != Say.Result.STATUS_COMPLETED:
                raise RuntimeError("failed")
        except Exception:
            self.get_logger().error("Localization help speech failed")


class VelocityGate(Node):
    def __init__(self):
        super().__init__("localization_velocity_gate")
        self.allowed_until = 0.0
        self.pub = self.create_publisher(Twist, "/diff_drive_controller/cmd_vel_unstamped", 10)
        self.create_subscription(String, "/localization/status", self.status, QOS)
        self.create_subscription(Twist, "/cmd_vel_localization_input", self.command, 10)
        self.create_timer(0.05, self.tick, clock=Clock(clock_type=ClockType.STEADY_TIME))

    def status(self, msg):
        try:
            data = json.loads(msg.data)
            allowed = (
                data.get("motion_blocked") is False
                and 0 <= time.monotonic() - data.get("heartbeat_monotonic", 0) < 0.3
            )
        except (ValueError, TypeError):
            allowed = False
        self.allowed_until = time.monotonic() + 0.3 if allowed else 0.0
        self.tick()

    def command(self, msg):
        self.pub.publish(msg if time.monotonic() < self.allowed_until else Twist())

    def tick(self):
        if time.monotonic() >= self.allowed_until:
            self.pub.publish(Twist())


def main(args=None):
    rclpy.init(args=args)
    node = LocalizationGuard()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


def gate_main(args=None):
    rclpy.init(args=args)
    node = VelocityGate()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
