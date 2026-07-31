"""Определение конца реплики.

Реализован уровень MVP: адаптивный таймаут тишины поверх Silero VAD,
подстраиваемый под признаки незавершённости в частичном транскрипте.
Семантический end-of-turn сюда встраивается позже как замена метода
_timeout_for(), не трогая интерфейс.

Обоснование очерёдности. Для экскурсовода вопросы посетителей короткие
и синтаксически простые. Адаптивный таймаут 600-800 мс закрывает MVP
целиком; семантический EOU экономит около 300 мс на длинных фразах
с паузой посередине. Это оптимизация, а не блокер, и делать её раньше
AEC -- потратить основной ресурс не туда.

Готовое, вопреки распространённому мнению, существует: smart-turn v2
от Pipecat (audio-based, Apache-2.0, экспортируется в ONNX) и text-based
детектор LiveKit. ROS-обёрток нет, но это обёртка, а не исследование.
Русский в обоих требует отдельной проверки.
"""

from __future__ import annotations

import rclpy
from guide_robot_msgs.msg import Transcript, VoiceActivity
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from std_msgs.msg import Empty

QOS_COMMAND = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE)

# Слова, после которых пауза почти наверняка не означает конец реплики.
CONTINUATION_HINTS: frozenset[str] = frozenset(
    """
    и а но или что чтобы который которая которые если когда потому хотя чем как где куда
    откуда зачем тоже также ещё то есть
    """.split()
)


class TurnDetector(LifecycleNode):
    """Адаптивный end-of-turn поверх VAD и частичного транскрипта."""

    def __init__(self) -> None:
        """Объявить параметры."""
        super().__init__("turn_detector")
        self.declare_parameter("base_timeout_ms", 700)
        self.declare_parameter("continuation_timeout_ms", 1400)
        self.declare_parameter("short_utterance_timeout_ms", 500)
        self.declare_parameter("short_utterance_words", 4)

        self._partial = ""
        self._silence_s = 0.0
        self._speaking = False
        self._fired = True

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Поднять интерфейсы."""
        del state
        self._turn_pub = self.create_lifecycle_publisher(Empty, "/speech/end_of_turn", QOS_COMMAND)
        self.create_subscription(
            VoiceActivity, "/speech/vad", self._on_vad, qos_profile_sensor_data
        )
        self.create_subscription(
            Transcript, "/asr/partial", self._on_partial, qos_profile_sensor_data
        )
        return TransitionCallbackReturn.SUCCESS

    def _on_partial(self, msg: Transcript) -> None:
        """Запомнить последнюю гипотезу."""
        self._partial = msg.text

    def _on_vad(self, msg: VoiceActivity) -> None:
        """Отследить длительность тишины и выдать конец реплики."""
        if msg.active:
            self._speaking = True
            self._fired = False
            self._silence_s = 0.0
            return
        if not self._speaking or self._fired:
            return

        self._silence_s = msg.state_duration
        if self._silence_s * 1e3 >= self._timeout_for(self._partial):
            self._fired = True
            self._speaking = False
            self._partial = ""
            self._turn_pub.publish(Empty())

    def _timeout_for(self, partial: str) -> float:
        """Подобрать порог тишины под содержимое гипотезы.

        Точка расширения: сюда встраивается семантический EOU,
        возвращающий порог по вероятности завершённости вместо эвристики.
        """
        words = partial.split()
        if not words:
            return float(self.get_parameter("base_timeout_ms").value)
        if words[-1].strip(",.!?").lower() in CONTINUATION_HINTS:
            return float(self.get_parameter("continuation_timeout_ms").value)
        if len(words) <= int(self.get_parameter("short_utterance_words").value):
            return float(self.get_parameter("short_utterance_timeout_ms").value)
        return float(self.get_parameter("base_timeout_ms").value)


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = TurnDetector()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
