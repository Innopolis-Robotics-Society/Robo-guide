# ruff: noqa: SLF001, PLR2004
# This adapter is part of the mission owner implementation and shares its lock.
"""Map-aware commands serialized with mission ownership in the ROS layer."""

import math
import threading
import time
from typing import Any

from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from guide_robot_msgs.action import GoToPose
from guide_robot_msgs.srv import ResetLocalization
from nav2_msgs.srv import ClearEntireCostmap
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.time import Time
from std_srvs.srv import Trigger, SetBool
from tf2_ros import Buffer, TransformException, TransformListener

from guide_robot_mission_control.fsm.blackboard_keys import Blackboard, TourPlan
from guide_robot_mission_control.fsm.root_sm import RootStateMachine
from guide_robot_mission_control.lib.map_geometry import OccupancyMap, quaternion_yaw
from guide_robot_mission_control.lib.qos import QOS_MISSION_STATE


class GoalAdapter:
    """Use existing FSM feedback and cancellation with the pose action."""

    def __init__(self, handle: Any) -> None:
        """Initialize the adapter and its configured resources."""
        self.handle = handle

    @property
    def is_cancel_requested(self) -> Any:
        """Expose cancellation to the shared mission FSM."""
        return self.handle.is_cancel_requested

    def publish_feedback(self, feedback: Any) -> Any:
        """Translate mission feedback to pose-action feedback."""
        self.handle.publish_feedback(GoToPose.Feedback(phase=feedback.phase))


class PoseControl:
    """Extra ROS interfaces sharing the owner's execution lock and context."""

    def __init__(self, owner: Any) -> None:
        """Initialize the adapter and its configured resources."""
        self.owner = owner
        self.map = None
        self.map_lock = threading.Lock()
        self.prepare_client = owner.create_client(Trigger, "/localization/prepare_reset")
        self.cleared_client = owner.create_client(SetBool, "/localization/costmaps_cleared")
        self.target = None
        self.target_pub = owner.create_publisher(
            PoseStamped, "/mission/target_pose", QOS_MISSION_STATE
        )
        self.buffer = Buffer(node=owner)
        self.listener = TransformListener(self.buffer, owner)
        self.map_subscription = owner.create_subscription(
            OccupancyGrid, "/map", self.on_map, QOS_MISSION_STATE
        )
        self.publisher = owner.create_publisher(PoseWithCovarianceStamped, "/initialpose", 10)
        self.clearers = [
            owner.create_client(ClearEntireCostmap, path, callback_group=owner._cb_reentrant)
            for path in (
                "/global_costmap/clear_entirely_global_costmap",
                "/local_costmap/clear_entirely_local_costmap",
            )
        ]
        self.action = ActionServer(
            owner,
            GoToPose,
            "/mission_fsm/go_to_pose",
            execute_callback=self.execute,
            goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=owner._cb_reentrant,
        )
        self.service = owner.create_service(
            ResetLocalization,
            "/mission_fsm/reset_localization",
            self.reset,
            callback_group=owner._cb_reentrant,
        )

    def on_map(self, msg: Any) -> Any:
        """Atomically replace the validated occupancy snapshot."""
        try:
            snapshot = OccupancyMap(msg)
        except ValueError:
            return
        with self.map_lock:
            self.map = snapshot

    def validate(self, request: Any, *, free: Any) -> Any:
        """Validate map identity, pose and navigation readiness."""
        if self.map is None:
            raise ValueError("map_unavailable")
        if request.pose.header.frame_id != self.map.frame:
            raise ValueError("invalid_frame")
        q = request.pose.pose.orientation
        norm = sum(v * v for v in (q.x, q.y, q.z, q.w))
        if not math.isfinite(norm) or abs(norm - 1) > 0.001 or abs(q.x) > 1e-6 or abs(q.y) > 1e-6:
            raise ValueError("invalid_orientation")
        p = request.pose.pose.position
        self.map.validate_pose(
            {"x": p.x, "y": p.y, "yaw": quaternion_yaw(q)}, request.map_version, free=free
        )
        if free:
            if self.owner.localization_blocked():
                raise ValueError("localization_required")
            try:
                transform = self.buffer.lookup_transform(self.map.frame, "base_link", Time())
                age = (
                    self.owner.get_clock().now() - Time.from_msg(transform.header.stamp)
                ).nanoseconds / 1e9
                if not 0 <= age <= 3:
                    raise ValueError("pose_stale")
            except TransformException as exc:
                raise ValueError("pose_unavailable") from exc
            if not self.owner._nav_client.server_is_ready():
                raise ValueError("navigation_unavailable")

    def reserve(self) -> Any:
        """Reserve mission ownership while holding the execution lock."""
        o = self.owner
        if (
            not o._active
            or o._active_ctx is not None
            or o._goal_reserved
            or o._safety_hold_event.is_set()
        ):
            return False
        o._goal_reserved = True
        o._pending_stop_requested = False
        return True

    def accept(self, request: Any) -> Any:
        """Atomically validate and reserve a pose action."""
        with self.owner._exec_lock, self.map_lock:
            try:
                self.validate(request, free=True)
            except ValueError:
                return GoalResponse.REJECT
            return GoalResponse.ACCEPT if self.reserve() else GoalResponse.REJECT

    def execute(self, handle: Any) -> Any:
        """Run one pose through the existing navigation state machine."""
        o = self.owner
        adapter = GoalAdapter(handle)
        outcome, detail = 2, ""
        try:
            with self.map_lock:
                self.validate(handle.request, free=True)
            self.target = handle.request.pose
            self.target_pub.publish(self.target)
            ctx = o._make_context(adapter, {"__pose_target": handle.request.pose})
            with o._exec_lock:
                o._active_ctx, o._active_goal_handle = ctx, adapter
                if o._pending_stop_requested:
                    ctx.request_stop()
            board = Blackboard(
                tour=TourPlan(
                    stop_ids=["__pose_target"],
                    exhibit_ids=[""],
                    greet=False,
                    narrate=False,
                    confirm_between_stops=False,
                    return_home=False,
                )
            )
            result = RootStateMachine(ctx).run_tour(board)
            outcome = (
                0 if result == "succeeded" else 1 if result in ("canceled", "shutdown") else 2
            )
            detail = result
            if outcome == 0 and (board.stops_completed != 1 or board.stops_skipped):
                outcome, detail = 2, "navigation_failed"
        except Exception as exc:
            o.get_logger().error(f"GoToPose failed: {exc}")
            detail = str(exc)
        finally:
            with o._exec_lock:
                o._active_ctx = o._active_goal_handle = None
                o._pending_stop_requested = False
                self.target = None
                self.target_pub.publish(PoseStamped())
                if o._active:
                    o._publish_idle_state()
                o._goal_reserved = False
        if handle.is_cancel_requested:
            handle.canceled()
        elif outcome == 0:
            handle.succeed()
        else:
            handle.abort()
        return GoToPose.Result(outcome=outcome, detail=detail)

    def reset(self, request: Any, response: Any) -> Any:
        """Publish a localization pose while excluding movement commands."""
        o = self.owner
        with o._exec_lock, self.map_lock:
            try:
                self.validate(request, free=False)
            except ValueError as exc:
                response.message = str(exc)
                return response
            if not self.reserve():
                response.message = "mission_busy_or_unsafe"
                return response
            try:
                if o.get_parameter("require_localization").value:
                    from guide_robot_mission_control.mission_fsm_node import _wait_future
                    if not self.prepare_client.service_is_ready():
                        raise ValueError("localization_monitor_unavailable")
                    future = self.prepare_client.call_async(Trigger.Request())
                    if not _wait_future(future, o.context, 2.) or not future.result().success:
                        raise ValueError("localization_monitor_unavailable")
                msg = PoseWithCovarianceStamped()
                msg.header = request.pose.header
                msg.header.stamp = o.get_clock().now().to_msg()
                msg.pose.pose = request.pose.pose
                for index, value in zip(
                    (0, 7, 35), o.get_parameter("reset_covariance_xyyaw").value, strict=True
                ):
                    msg.pose.covariance[index] = value
                self.publisher.publish(msg)
                response.accepted = True
            except ValueError as exc:
                o._goal_reserved = False
                response.message = str(exc)
                return response
            except Exception:
                o._goal_reserved = False
                raise
        try:
            from guide_robot_mission_control.mission_fsm_node import _wait_future  # noqa: PLC0415

            time.sleep(max(0.0, float(o.get_parameter("initialpose_settle_s").value)))
            response.costmaps_cleared = True
            for client in self.clearers:
                if not client.service_is_ready():
                    response.costmaps_cleared = False
                    continue
                future = client.call_async(ClearEntireCostmap.Request())
                try:
                    if not _wait_future(future, o.context, 3.0) or future.result() is None:
                        response.costmaps_cleared = False
                except Exception:
                    response.costmaps_cleared = False
            if o.get_parameter("require_localization").value:
                future = self.cleared_client.call_async(SetBool.Request(data=response.costmaps_cleared))
                if not _wait_future(future, o.context, 2.) or not future.result().success:
                    response.costmaps_cleared = False
            response.message = (
                "pose_published"
                if response.costmaps_cleared
                else "pose_published_costmaps_unconfirmed"
            )
        finally:
            with o._exec_lock:
                o._goal_reserved = False
        return response

    def close(self) -> None:
        """Release action waitables and TF subscriptions before destroying the node."""
        self.action.destroy()
        self.listener.unregister()
        self.owner.destroy_service(self.service)
        self.owner.destroy_subscription(self.map_subscription)
        for client in self.clearers:
            self.owner.destroy_client(client)
        self.owner.destroy_client(self.prepare_client)
        self.owner.destroy_client(self.cleared_client)
        self.owner.destroy_publisher(self.publisher)
        self.owner.destroy_publisher(self.target_pub)
