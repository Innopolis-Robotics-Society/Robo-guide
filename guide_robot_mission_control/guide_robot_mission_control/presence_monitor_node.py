"""presence_monitor -- агрегирует разрозненные свидетельства присутствия в `/mission/presence`.

design §6. Источники -- реальные топики guide_robot_voice: `/speech/wakeword`
(`Wakeword`), `/asr/transcript` (`Transcript`, только `is_final`), `/vad`
(`VoiceActivity`) -- design-черновик называл их `/voice/wakeword`,
`/asr/final`, `/voice/vad` до реконсиляции §0.5, здесь используются
реальные имена по той же логике, что и в narration_server. `/perception/people`
не подключается вовсе: под него в `guide_robot_msgs` нет типа сообщения --
"не ломаться при его отсутствии" (design §6) для несуществующего топика
означает просто не создавать подписку, а не защищаться от неё рантаймом.

Решение "считать ли это свидетельство" и "present ли сейчас" живёт в
presence.py (чистый Python, без ROS) -- узел только раскладывает ROS-msg по
полям чистых функций и публикует результат.
"""

from __future__ import annotations

import rclpy
from guide_robot_msgs.msg import (
    MissionState,
    Presence,
    SpeakingStatus,
    Transcript,
    VoiceActivity,
    Wakeword,
)
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn

from guide_robot_mission_control.lib.qos import (
    QOS_ASR_TRANSCRIPT,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
    QOS_VAD,
    QOS_VOICE_SPEAKING,
    QOS_WAKEWORD,
)
from guide_robot_mission_control.presence import PresenceTracker, vad_evidence_allowed

__all__ = ["PresenceMonitorNode", "main"]


class PresenceMonitorNode(LifecycleNode):
    """Lifecycle-нода: подписки на свидетельства + heartbeat-паблишер `/mission/presence`."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Ресурсы ROS захватываются в on_configure."""
        super().__init__("presence_monitor", **node_kwargs)

        self.declare_parameter("disengage_timeout_s", 120.0)
        self.declare_parameter("wakeword_min_confidence", 0.6)
        self.declare_parameter("ignore_vad_while_speaking", True)
        self.declare_parameter("tts_tail_ms", 300.0)
        self.declare_parameter("sources", ["wakeword", "asr_final", "vad"])
        self.declare_parameter("publish_rate_hz", 1.0)
        # Design §6 упоминает /mission/state как опциональное слабое
        # свидетельство с weak_evidence:false по умолчанию, но не
        # специфицирует параметр в §8 -- имя ниже наше, задокументировано
        # как расширение, не буквальный design.
        self.declare_parameter("mission_state_weak_evidence", False)

        self._active = False
        self._disengage_timeout_s = 120.0
        self._wakeword_min_confidence = 0.6
        self._ignore_vad_while_speaking = True
        self._tts_tail_s = 0.3
        self._sources: set[str] = set()
        self._publish_rate_hz = 1.0
        self._mission_state_weak_evidence = False

        self._tracker = PresenceTracker(disengage_timeout_s=120.0)
        self._speaking = False
        self._speaking_ended_ns: int | None = None
        self._last_stop_index: int | None = None

        self._cb_sub = MutuallyExclusiveCallbackGroup()

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять подписки по включённым источникам и паблишер."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        self._disengage_timeout_s = float(self.get_parameter("disengage_timeout_s").value)
        self._wakeword_min_confidence = float(self.get_parameter("wakeword_min_confidence").value)
        self._ignore_vad_while_speaking = bool(
            self.get_parameter("ignore_vad_while_speaking").value
        )
        self._tts_tail_s = float(self.get_parameter("tts_tail_ms").value) / 1000.0
        self._sources = {str(s) for s in self.get_parameter("sources").value}
        self._publish_rate_hz = float(self.get_parameter("publish_rate_hz").value)
        self._mission_state_weak_evidence = bool(
            self.get_parameter("mission_state_weak_evidence").value
        )
        self._tracker = PresenceTracker(disengage_timeout_s=self._disengage_timeout_s)

        if "wakeword" in self._sources:
            self._wakeword_sub = self.create_subscription(
                Wakeword, "/speech/wakeword", self._on_wakeword, QOS_WAKEWORD,
                callback_group=self._cb_sub,
            )
        if "asr_final" in self._sources:
            self._transcript_sub = self.create_subscription(
                Transcript, "/asr/transcript", self._on_transcript, QOS_ASR_TRANSCRIPT,
                callback_group=self._cb_sub,
            )
        if "vad" in self._sources:
            self._vad_sub = self.create_subscription(
                VoiceActivity, "/vad", self._on_vad, QOS_VAD, callback_group=self._cb_sub,
            )
            self._speaking_sub = self.create_subscription(
                SpeakingStatus, "/voice/speaking", self._on_speaking_status, QOS_VOICE_SPEAKING,
                callback_group=self._cb_sub,
            )
        if self._mission_state_weak_evidence:
            self._mission_state_sub = self.create_subscription(
                MissionState, "/mission/state", self._on_mission_state, QOS_MISSION_STATE,
                callback_group=self._cb_sub,
            )

        self._presence_pub = self.create_lifecycle_publisher(
            Presence, "/mission/presence", QOS_MISSION_PRESENCE
        )

        self.get_logger().info(
            f"presence_monitor сконфигурирован: sources={sorted(self._sources)}, "
            f"disengage_timeout_s={self._disengage_timeout_s}"
        )
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Разрешить обработку свидетельств и запустить heartbeat-таймер публикации."""
        self._active = True
        self._publish_timer = self.create_timer(
            1.0 / self._publish_rate_hz, self._publish_presence
        )
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Остановить таймер и перестать обрабатывать новые свидетельства."""
        self._active = False
        self.destroy_timer(self._publish_timer)
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить накопленное состояние между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup -- отдельного пути завершения активной работы не требуется."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        self._speaking = False
        self._speaking_ended_ns = None
        self._last_stop_index = None

    # -- источники свидетельств ----------------------------------------------

    def _on_wakeword(self, msg: Wakeword) -> None:
        if not self._active:
            return
        if msg.confidence < self._wakeword_min_confidence:
            return
        self._record(source="wakeword")

    def _on_transcript(self, msg: Transcript) -> None:
        if not self._active:
            return
        if not msg.is_final:
            return
        self._record(source="asr_final")

    def _on_speaking_status(self, msg: SpeakingStatus) -> None:
        """Копится независимо от self._active -- гейт VAD не теряет хвост TTS при рестарте."""
        now_ns = self.get_clock().now().nanoseconds
        was_speaking = self._speaking
        self._speaking = bool(msg.speaking)
        if was_speaking and not self._speaking:
            self._speaking_ended_ns = now_ns

    def _on_vad(self, msg: VoiceActivity) -> None:
        if not self._active:
            return
        if not msg.active:
            return
        now_ns = self.get_clock().now().nanoseconds
        seconds_since_speaking_ended = (
            (now_ns - self._speaking_ended_ns) / 1e9
            if self._speaking_ended_ns is not None
            else None
        )
        allowed = vad_evidence_allowed(
            ignore_vad_while_speaking=self._ignore_vad_while_speaking,
            speaking=self._speaking,
            seconds_since_speaking_ended=seconds_since_speaking_ended,
            tts_tail_s=self._tts_tail_s,
        )
        if not allowed:
            return
        self._record(source="vad", now_ns=now_ns)

    def _on_mission_state(self, msg: MissionState) -> None:
        if not self._active:
            return
        if self._last_stop_index == msg.stop_index:
            return
        self._last_stop_index = msg.stop_index
        self._record(source="mission_state")

    def _record(self, *, source: str, now_ns: int | None = None) -> None:
        if now_ns is None:
            now_ns = self.get_clock().now().nanoseconds
        self._tracker.record_evidence(now_ns=now_ns, source=source)

    # -- публикация -----------------------------------------------------

    def _publish_presence(self) -> None:
        now = self.get_clock().now()
        now_ns = now.nanoseconds
        msg = Presence()
        msg.header.stamp = now.to_msg()
        msg.present = self._tracker.present(now_ns=now_ns)
        msg.seconds_since_evidence = float(self._tracker.seconds_since_evidence(now_ns=now_ns))
        msg.last_source = self._tracker.last_source
        if self._tracker.last_evidence_ns is not None:
            evidence_ns = self._tracker.last_evidence_ns
            msg.last_evidence.sec = int(evidence_ns // 1_000_000_000)
            msg.last_evidence.nanosec = int(evidence_ns % 1_000_000_000)
        self._presence_pub.publish(msg)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = PresenceMonitorNode()
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
