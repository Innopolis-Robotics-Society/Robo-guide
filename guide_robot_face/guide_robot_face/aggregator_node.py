"""face_aggregator -- публикует /face/state из сигналов пайплайна.

Не знает про LLM-affect и не вызывает /face/set_state: face_node сам
читает /face/state. Логика выбора -- ``lib.aggregator.decide``.
"""

from __future__ import annotations

import time
from dataclasses import replace

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from guide_robot_face.lib.aggregator import MISSION_NAVIGATING, FaceInputs, decide
from guide_robot_face.lib.qos import (
    QOS_DIALOG_PHASE,
    QOS_FACE_STATE,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
    QOS_VAD,
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
    Wakeword,
)

_SPEAKING_STALE_SEC = 0.4
_WAKEWORD_HOLD_SEC = 2.0
_TICK_SEC = 0.2


class FaceAggregatorNode(Node):
    """Подписки на пайплайн, публикация /face/state при смене решения."""

    def __init__(self) -> None:
        """Подписаться на входы и один раз опубликовать стартовое состояние."""
        super().__init__("face_aggregator")
        self._inp = FaceInputs()
        self._latest_speaking: SpeakingStatus | None = None
        self._wakeword_until = 0.0
        self._seq = 0
        self._last_state = ""

        self._pub = self.create_publisher(FaceState, "/face/state", QOS_FACE_STATE)
        self.create_subscription(String, "/supervisor/state", self._on_supervisor, 10)
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
        self.create_timer(_TICK_SEC, self._tick)
        self._publish_if_changed()

    def _on_supervisor(self, msg: String) -> None:
        self._inp = replace(self._inp, supervisor_fault=msg.data == "FAULT")
        self._publish_if_changed()

    def _on_speaking(self, msg: SpeakingStatus) -> None:
        self._latest_speaking = msg
        self._sync_ephemeral()
        self._publish_if_changed()

    def _on_phase(self, msg: DialogPhase) -> None:
        self._inp = replace(self._inp, dialog_phase=int(msg.phase))
        self._publish_if_changed()

    def _on_mission(self, msg: MissionState) -> None:
        self._inp = replace(self._inp, navigating=int(msg.state) == MISSION_NAVIGATING)
        self._publish_if_changed()

    def _on_presence(self, msg: Presence) -> None:
        self._inp = replace(self._inp, presence=bool(msg.present))
        self._publish_if_changed()

    def _on_vad(self, msg: VoiceActivity) -> None:
        active = bool(msg.active)
        if active == self._inp.vad_active:
            return
        self._inp = replace(self._inp, vad_active=active)
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
        self._inp = replace(
            self._inp,
            speaking=self._speaking_now(),
            wakeword_hold=time.monotonic() < self._wakeword_until,
        )

    def _publish_if_changed(self) -> None:
        state = decide(self._inp)
        if state == self._last_state:
            return
        self._seq += 1
        self._last_state = state
        self._pub.publish(FaceState(state=state, gaze_az=0.0, seq=self._seq))


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
