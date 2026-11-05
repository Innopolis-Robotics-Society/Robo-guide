"""Head tracking decisions with mocked sound/encoders, no physical motion."""
import time
from unittest.mock import patch

import pytest
rclpy = pytest.importorskip('rclpy')
from guide_robot_voice.sound_head_node import SoundHeadNode


class CapturePublisher:
    def __init__(self):
        self.values = []

    def publish(self, msg):
        self.values.append(msg.data)


def test_fresh_encoder_required_and_target_is_bounded():
    rclpy.init()
    node = SoundHeadNode()
    node.usb_fd = -1
    node.publisher = CapturePublisher()
    try:
        with patch('guide_robot_voice.sound_head_node.read_doa', return_value=(90, 1)):
            node.tick()
            assert node.publisher.values == []
            now = time.monotonic()
            for i in range(5):
                node.feedback.sample(now - 1.0 + i * .2, 1780)
            node.history.extend([90, 90, 90])
            node.hold_side = 1
            node.hold_since = now - 1
            node.tick()
            assert node.publisher.values == [1550]
            node.tick()
            assert node.publisher.values == [1550]
            node.feedback.samples.clear()
            node.history.extend([270, 270, 270])
            node.hold_side = -1
            node.hold_since = now - 1
            node.tick()
            assert node.publisher.values == [1550]
    finally:
        node.usb_fd = None
        node.destroy_node()
        rclpy.shutdown()
