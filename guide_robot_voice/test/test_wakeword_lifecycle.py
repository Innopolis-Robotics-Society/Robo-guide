"""Повторный configure не оставляет старые publishers/subscriptions в lifecycle."""

from pathlib import Path

import pytest
import rclpy
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter

from guide_robot_voice.voice_session_manager import VoiceSessionManager
from guide_robot_voice.wakeword_node import WakewordNode


@pytest.mark.parametrize("kind", ["manager", "wakeword"])
def test_reconfigure_releases_all_managed_interfaces(kind):
    rclpy.init()
    node = VoiceSessionManager() if kind == "manager" else WakewordNode()
    try:
        if kind == "manager":
            node.set_parameters(
                [
                    Parameter("wakeword_gated", value=True),
                    Parameter("wakeword_model_sha256", value="frozen"),
                ]
            )
        else:
            pytest.importorskip("openwakeword")
            bundle = Path(__file__).resolve().parents[1] / "models/firaya"
            if not bundle.exists():
                pytest.skip("Deploy bundle not present in this checkout")
            node.set_parameters(
                [
                    Parameter("backend", value="oww"),
                    Parameter("model_bundle_path", value=str(bundle)),
                ]
            )
        initial_publishers = len(list(node.publishers))
        for _ in range(2):
            assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
            assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
            assert node.trigger_deactivate() == TransitionCallbackReturn.SUCCESS
            assert node.trigger_cleanup() == TransitionCallbackReturn.SUCCESS
            assert len(list(node.publishers)) == initial_publishers
            assert len(list(node.subscriptions)) == 0
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
