"""face_aggregator -- публикует /face/state из сигналов пайплайна.

Не знает про LLM-affect и не вызывает /face/set_state: face_node сам
читает /face/state. Логика выбора -- ``lib.aggregator.decide``.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from pathlib import Path

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from std_msgs.msg import Bool, String

from guide_robot_face.lib.aggregator import (
    MISSION_NAVIGATING,
    MISSION_RETURNING,
    FaceInputs,
    decide,
)
from guide_robot_face.lib.qos import (
    QOS_DIALOG_PHASE,
    QOS_FACE_STATE,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
    QOS_SUPERVISOR_STATE,
    QOS_VAD,
    QOS_VOICE_SESSION_STATE,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_msgs.msg import (
    DialogPhase,
    FaceState,
    MissionState,
    Presence,
    SpeakingStatus,
    VoiceActivity,
    VoiceSessionState,
    Wakeword,
)
from guide_robot_msgs.srv import OverrideFace

_SPEAKING_STALE_SEC = 0.4
_WAKEWORD_HOLD_SEC = 2.0
_TICK_SEC = 0.2


class FaceAggregatorNode(Node):
    """Подписки на пайплайн, публикация /face/state при смене решения."""

    def __init__(self) -> None:
        """Подписаться на входы и один раз опубликовать стартовое состояние."""
        super().__init__("face_aggregator")
        self.declare_parameter("session_managed_listening", False)
        self._inp = FaceInputs(
            session_managed=bool(self.get_parameter("session_managed_listening").value)
        )
        self._latest_session: VoiceSessionState | None = None
        self._latest_speaking: SpeakingStatus | None = None
        self._wakeword_until = 0.0
        states = yaml.safe_load(
            (
                Path(get_package_share_directory("guide_robot_face")) / "config/face_states.yaml"
            ).read_text()
        )
        self._expressions = set(states["states"])
        self._override = None
        self.create_service(OverrideFace, "/face/override", self._set_override)
        self._estop = False
        self._supervisor_fault = False
        self.create_subscription(Bool, "/supervisor/estop", self._on_estop, QOS_FACE_STATE)
        self._seq = 0
        self._last_state = ""

        self._pub = self.create_publisher(FaceState, "/face/state", QOS_FACE_STATE)
        self.create_subscription(
            String, "/supervisor/state", self._on_supervisor, QOS_SUPERVISOR_STATE
        )
        self.create_subscription(
            SpeakingStatus, "/voice/speaking", self._on_speaking, QOS_VOICE_SPEAKING
        )
        self.create_subscription(DialogPhase, "/dialog/phase", self._on_phase, QOS_DIALOG_PHASE)
        self.create_subscription(
            MissionState, "/mission/state", self._on_mission, QOS_MISSION_STATE
        )
        self.create_subscription(
            Presence, "/mission/presence", self._on_presence, QOS_MISSION_PRESENCE
        )
        self.create_subscription(VoiceActivity, "/vad", self._on_vad, QOS_VAD)
        self.create_subscription(Wakeword, "/speech/wakeword", self._on_wakeword, QOS_WAKEWORD)
        self.create_subscription(
            VoiceSessionState, "/voice/session_state", self._on_session, QOS_VOICE_SESSION_STATE
        )
        self.create_timer(_TICK_SEC, self._tick, clock=Clock(clock_type=ClockType.STEADY_TIME))
        self._publish_if_changed()

    def _on_supervisor(self, msg: String) -> None:
        self._supervisor_fault = msg.data in ("FAULT", "SHUTDOWN")
        self._inp = replace(self._inp, supervisor_fault=self._supervisor_fault or self._estop)
        self._publish_if_changed()

    def _on_estop(self, msg):
        self._estop = bool(msg.data)
        self._inp = replace(self._inp, supervisor_fault=self._supervisor_fault or self._estop)
        self._publish_if_changed()

    def _on_speaking(self, msg: SpeakingStatus) -> None:
        self._latest_speaking = msg
        self._sync_ephemeral()
        self._publish_if_changed()

    def _on_phase(self, msg: DialogPhase) -> None:
        self._inp = replace(self._inp, dialog_phase=int(msg.phase))
        self._publish_if_changed()

    def _on_mission(self, msg: MissionState) -> None:
        moving = int(msg.state) in (MISSION_NAVIGATING, MISSION_RETURNING) or (
            msg.state == MissionState.STATE_ANSWERING
            and msg.base_state == MissionState.STATE_NAVIGATING
        )
        self._inp = replace(self._inp, navigating=moving)
        self._publish_if_changed()

    def _on_presence(self, msg: Presence) -> None:
        self._inp = replace(self._inp, presence=bool(msg.present))
        self._publish_if_changed()

    def _on_vad(self, msg: VoiceActivity) -> None:
        if self._inp.session_managed:
            return
        active = bool(msg.active)
        if active == self._inp.vad_active:
            return
        self._inp = replace(self._inp, vad_active=active)
        self._publish_if_changed()

    def _on_session(self, msg: VoiceSessionState) -> None:
        if not self._inp.session_managed:
            return
        previous = self._latest_session
        stamp = msg.stamp.sec + msg.stamp.nanosec / 1e9
        if previous is not None:
            previous_stamp = previous.stamp.sec + previous.stamp.nanosec / 1e9
            if stamp < previous_stamp:
                return
        self._latest_session = msg
        self._sync_ephemeral()
        self._publish_if_changed()

    def _on_wakeword(self, _msg: Wakeword) -> None:
        self._wakeword_until = time.monotonic() + _WAKEWORD_HOLD_SEC
        self._sync_ephemeral()
        self._publish_if_changed()

    def _tick(self) -> None:
        self._sync_ephemeral()
        self._publish_if_changed()

    def _speaking_now(self) -> bool:
        status = self._latest_speaking
        if status is None or not status.speaking:
            return False
        stamp = status.stamp.sec + status.stamp.nanosec / 1e9
        age = self.get_clock().now().nanoseconds / 1e9 - stamp
        return age <= _SPEAKING_STALE_SEC

    def _sync_ephemeral(self) -> None:
        session = self._latest_session
        age = (
            float("inf")
            if session is None
            else (
                self.get_clock().now().nanoseconds / 1e9
                - session.stamp.sec
                - session.stamp.nanosec / 1e9
            )
        )
        fresh = -0.1 <= age <= 0.6
        self._inp = replace(
            self._inp,
            speaking=self._speaking_now(),
            wakeword_hold=time.monotonic() < self._wakeword_until,
            session_listening=bool(
                fresh
                and session is not None
                and session.state
                in (
                    VoiceSessionState.STATE_LISTENING,
                    VoiceSessionState.STATE_USER_SPEAKING,
                    VoiceSessionState.STATE_STOPPING_OUTPUT,
                )
            ),
            session_recognizing=bool(
                fresh
                and session is not None
                and session.state == VoiceSessionState.STATE_RECOGNIZING
            ),
        )

    def _effective(self):
        automatic = decide(self._inp)
        if self._override and time.monotonic() >= self._override[2]:
            self._override = None
        if self._override and automatic not in ("error", "driving"):
            return self._override[0], self._override[1], "override"
        return automatic, 0.0, "automatic"

    def _set_override(self, request, response):
        if request.state and (
            request.state not in self._expressions
            or not math.isfinite(request.duration_s)
            or not 0 < request.duration_s <= 60
            or not math.isfinite(request.gaze_az)
            or not -90 <= request.gaze_az <= 90
        ):
            response.accepted = False
            response.message = "invalid_expression"
            return response
        self._override = (
            (request.state, request.gaze_az, time.monotonic() + request.duration_s)
            if request.state
            else None
        )
        self._publish_if_changed()
        response.accepted = True
        response.effective_state, _, response.source = self._effective()
        return response

    def _publish_if_changed(self) -> None:
        state, gaze, source = self._effective()
        key = (state, gaze, source)
        if key == self._last_state:
            return
        self._seq += 1
        self._last_state = key
        self._pub.publish(FaceState(state=state, gaze_az=gaze, seq=self._seq, source=source))


def main(args: list[str] | None = None) -> None:
    """Точка входа console_script face_aggregator."""
    rclpy.init(args=args)
    node = FaceAggregatorNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
