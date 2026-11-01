"""Orient the FURO-D head toward speech; the motor driver owns the COM port."""

import collections
import math
import os
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int32

from .head_feedback import HeadFeedback, can_retarget, compensated_target
from .xvf3800_control import configure_direction_led, open_dev, read_doa


def wrap180(value):
    return (value + 180.0) % 360.0 - 180.0


class SoundHeadNode(Node):
    def __init__(self):
        super().__init__('sound_head_node')
        self.publisher = self.create_publisher(Int32, '/furo/head_target_raw', 1)
        self.subscription = self.create_subscription(
            Int32, '/furo/head_encoder_raw', self.encoder,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
        self.feedback = HeadFeedback()
        self.history = collections.deque(maxlen=3)
        self.usb_fd = None
        self.next_open = 0.0
        self.started = time.monotonic()
        self.sent_deg = None
        self.sent_side = 0
        self.last_send = 0.0
        self.hold_side = 0
        self.hold_since = 0.0
        self.timer = self.create_timer(0.04, self.tick)
        self.get_logger().info('Слежение за речью: ожидание микрофона и энкодера головы')

    def encoder(self, msg):
        now = time.monotonic()
        if now - self.started >= 2.0 and 1250 <= msg.data <= 2450:
            self.feedback.sample(now, msg.data)

    def close_usb(self):
        if self.usb_fd is not None:
            os.close(self.usb_fd)
            self.usb_fd = None

    def tick(self):
        now = time.monotonic()
        if self.usb_fd is None:
            if now < self.next_open:
                return
            self.next_open = now + 5.0
            try:
                self.usb_fd = open_dev()
                configure_direction_led(self.usb_fd)
                self.history.clear()
                self.get_logger().info('reSpeaker: красный круг, белое направление')
            except (OSError, IOError, SystemExit) as exc:
                self.get_logger().warning(f'reSpeaker недоступен: {exc}')
                self.close_usb()
                return
        try:
            angle, vad = read_doa(self.usb_fd)
        except (OSError, IOError) as exc:
            self.get_logger().warning(f'Ошибка чтения reSpeaker: {exc}')
            self.close_usb()
            self.next_open = time.monotonic() + 5.0
            self.history.clear()
            return

        position = self.feedback.position(now)
        if position is None:
            return
        if self.sent_deg is None:
            self.sent_deg = -(position[0] - 1780) / 20.0
        if not vad:
            self.history.clear()
            self.hold_side = 0
            self.hold_since = now
            return
        self.history.append(angle)
        if len(self.history) < self.history.maxlen:
            return
        rads = [math.radians(a) for a in self.history]
        mean = math.degrees(math.atan2(sum(map(math.sin, rads)), sum(map(math.cos, rads)))) % 360
        if max(abs(wrap180(a - mean)) for a in self.history) > 12:
            return
        bearing = wrap180(-(mean - 180.0))
        side = 0 if abs(bearing) < 2 else (1 if bearing > 0 else -1)
        if side != self.hold_side:
            self.hold_side, self.hold_since = side, now
        if now - self.hold_since < 0.18:
            return
        if self.last_send and side != self.sent_side and now - self.last_send < 0.6:
            return
        if self.last_send and not can_retarget(side, self.sent_side, now - self.last_send, 2.5):
            return
        normalized = max(-1.0, min(1.0, bearing / 50.0))
        wanted = 20.0 * math.copysign(abs(normalized) ** 0.6, normalized)
        raw = max(1550, min(2180, 1780 - round(wanted * 20)))
        effective = -(raw - 1780) / 20.0
        if abs(effective - self.sent_deg) < 3:
            return
        command = compensated_target(raw, position[0], 1550, 2205)
        msg = Int32()
        msg.data = command
        self.publisher.publish(msg)
        self.feedback.command(raw)
        self.sent_deg, self.sent_side, self.last_send = effective, side, now
        self.get_logger().info(
            f'пеленг={bearing:+.1f}° цель={raw} команда={command} энкодер={position[0]:.0f}')

    def destroy_node(self):
        self.close_usb()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = SoundHeadNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
