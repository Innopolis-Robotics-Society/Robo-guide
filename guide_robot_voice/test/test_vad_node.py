"""vad_node: подавление автоматического barge-in для непрерываемой речи (stage4 §2.2).

Гоняет реальную ноду (реальная модель silero_vad.onnx, git-lfs) с
barge_in_min_windows=1, чтобы не городить синтетический аудиопоток --
`_maybe_trigger_barge_in()` вызывается напрямую с уже готовой вероятностью,
а `/voice/speaking` -- через `_on_speaking_status()`, то же самое, что
реальная подписка получила бы от tts_node.
"""

from __future__ import annotations

import pytest
import rclpy
from ament_index_python.packages import get_package_share_directory
from guide_robot_msgs.msg import SpeakingStatus
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter

from guide_robot_voice.vad_node import VadNode

_MODEL_PATH = f"{get_package_share_directory('guide_robot_voice')}/models/silero_vad.onnx"
_ABOVE_THRESHOLD = 0.99


@pytest.fixture
def node():
    rclpy.init()
    n = VadNode()
    n.set_parameters(
        [
            Parameter("model_path", value=_MODEL_PATH),
            Parameter("barge_in_min_windows", value=1),
        ]
    )
    assert n.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert n.trigger_activate() == TransitionCallbackReturn.SUCCESS
    yield n
    n.destroy_node()
    rclpy.try_shutdown()


def _speaking_status(node: VadNode, *, speaking: bool, interruptible: bool) -> SpeakingStatus:
    msg = SpeakingStatus()
    msg.stamp = node.get_clock().now().to_msg()
    msg.speaking = speaking
    msg.interruptible = interruptible
    return msg


def test_barge_in_suppressed_when_active_speech_is_not_interruptible(node: VadNode) -> None:
    node._on_speaking_status(_speaking_status(node, speaking=True, interruptible=False))
    before = node._barge_in_triggers_total

    node._maybe_trigger_barge_in(0.0, _ABOVE_THRESHOLD)

    assert node._barge_in_triggers_total == before


def test_barge_in_fires_when_active_speech_is_interruptible(node: VadNode) -> None:
    node._on_speaking_status(_speaking_status(node, speaking=True, interruptible=True))
    before = node._barge_in_triggers_total

    node._maybe_trigger_barge_in(0.0, _ABOVE_THRESHOLD)

    assert node._barge_in_triggers_total == before + 1


def test_barge_in_suppressed_only_while_speaking_status_is_fresh(node: VadNode) -> None:
    """Протухший статус уже не "говорит" вовсе (design §2) -- гейт по interruptible неважен."""
    msg = _speaking_status(node, speaking=True, interruptible=False)
    msg.stamp.sec -= 10  # старше _SPEAKING_STATUS_STALE_SEC (0.4с) на порядки
    node._on_speaking_status(msg)
    before = node._barge_in_triggers_total

    node._maybe_trigger_barge_in(0.0, _ABOVE_THRESHOLD)

    # Не подавлено -- протухший статус не защищает: _is_tts_speaking()
    # сам по себе False, значит и обычный (не только новый) гейт молчит.
    assert node._barge_in_triggers_total == before


def test_barge_in_stays_suppressed_when_tts_not_speaking_at_all(node: VadNode) -> None:
    """Контроль: TTS молчит -- барж-ину и так нечего прерывать, вне зависимости от флага."""
    node._on_speaking_status(_speaking_status(node, speaking=False, interruptible=False))
    before = node._barge_in_triggers_total

    node._maybe_trigger_barge_in(0.0, _ABOVE_THRESHOLD)

    assert node._barge_in_triggers_total == before
