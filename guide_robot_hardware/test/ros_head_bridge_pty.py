#!/usr/bin/env python3
"""ROS integration check against a PTY controller; never opens robot hardware."""
import os
import pty
import select
import signal
import struct
import subprocess
import tempfile
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int32


def frame(head):
    body = bytes.fromhex('2e 0f 47 a0') + struct.pack('<iii', 0, 0, head)
    return b'\xff\xff' + body + bytes([~sum(body) & 255])


def main():
    master, slave = pty.openpty()
    device = os.ttyname(slave)
    params = dict(serial_port=device, baud_rate=115200, left_wheel_id=46, right_wheel_id=47,
                  left_sign=1, right_sign=-1, speed_coefficient=0.0000880243,
                  speed_offset=0.0436513, low_speed_coefficient=0.0001706,
                  left_speed_trim=0.99, cmd_timeout=0.3, max_wheel_velocity=7.31,
                  encoder_timeout=4.0, encoder_poll_divider=5)
    hp = ''.join(f'<param name="{k}">{v}</param>' for k, v in params.items())
    joints = ''.join(f'<joint name="{n}"><command_interface name="velocity"/>'
                     '<state_interface name="position"/><state_interface name="velocity"/>'
                     '</joint>' for n in ('left_wheel_joint', 'right_wheel_joint'))
    urdf = ('<robot name="test"><ros2_control name="TestSystem" type="system">'
            '<hardware><plugin>guide_robot_hardware/GuideRobotSystem</plugin>' + hp +
            '</hardware>' + joints + '</ros2_control></robot>')
    packets = []
    packet_times = []
    state = {'run': True, 'feedback': True}

    def emulator():
        buf = bytearray(); next_frame = time.monotonic()
        while state['run']:
            now = time.monotonic()
            if state['feedback'] and now >= next_frame:
                try:
                    os.write(master, frame(1780))
                except OSError:
                    pass
                next_frame = now + 0.2
            if select.select([master], [], [], .02)[0]:
                try:
                    buf.extend(os.read(master, 4096))
                except OSError:
                    continue
                while len(buf) >= 4:
                    if buf[:2] != b'\xff\xff':
                        del buf[0]; continue
                    n = buf[3] + 4
                    if len(buf) < n:
                        break
                    packets.append(bytes(buf[:n])); packet_times.append(time.monotonic()); del buf[:n]

    thread = threading.Thread(target=emulator, daemon=True)
    thread.start()
    env = os.environ.copy()
    env['ROS_DOMAIN_ID'] = '189'
    env['ROS_LOCALHOST_ONLY'] = '1'
    os.environ.update(ROS_DOMAIN_ID='189', ROS_LOCALHOST_ONLY='1')
    with tempfile.TemporaryFile(mode='w+') as log:
        proc = subprocess.Popen(['ros2', 'run', 'controller_manager', 'ros2_control_node',
                                 '--ros-args', '-p', 'robot_description:=' + urdf,
                                 '-p', 'update_rate:=20'], stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        rclpy.init()
        node = Node('head_bridge_pty_test')
        seen = []
        sub = node.create_subscription(Int32, '/furo/head_encoder_raw', lambda m: seen.append(m.data),
                                       QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT))
        pub = node.create_publisher(Int32, '/furo/head_target_raw', 1)

        def pump(seconds, value=None):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if value is not None:
                    m = Int32(); m.data = value; pub.publish(m)
                rclpy.spin_once(node, timeout_sec=0.05)
                if proc.poll() is not None:
                    raise RuntimeError('ros2_control_node exited')

        try:
            pump(5)
            assert seen and seen[-1] == 1780, f'no encoder telemetry: {seen[-3:]}'
            head_packets = lambda: [p for p in packets if len(p) == 13 and p[4:8] == b'\x83\x1e\x04\x2b']
            initial = len(head_packets())
            pump(0.7, 0)
            assert len(head_packets()) == initial, 'out-of-range command was sent'
            burst_start = time.monotonic()
            pump(1.0, 1780)
            burst_end = time.monotonic()
            valid = head_packets()[initial:]
            assert valid, 'valid command not sent'
            assert all(p == bytes.fromhex('ff ff fe 09 83 1e 04 2b f4 06 0a 00 24') for p in valid), valid
            state['feedback'] = False
            pump(1.5)
            count = len(head_packets())
            pump(.7, 1900)
            assert len(head_packets()) == count, 'command sent without fresh encoder'
            wheels = [p for p in packets if len(p) == 18 and p[4:7] == b'\x06\x20\x04']
            assert wheels and all(p[8:10] == b'\0\0' and p[13:15] == b'\0\0' for p in wheels), 'unexpected wheel motion packet'
            wheel_times = [t for p, t in zip(packets, packet_times)
                           if burst_start <= t <= burst_end and len(p) == 18 and
                           p[4:7] == b'\x06\x20\x04']
            assert len(wheel_times) >= 10, f'wheel loop stopped during head burst: {len(wheel_times)}'
            max_gap = max(b - a for a, b in zip(wheel_times, wheel_times[1:]))
            assert max_gap < .3, f'wheel loop delayed by {max_gap:.3f}s during head burst'
            print(f'PASS: encoder={seen[-1]}, head packets={len(valid)}, wheel zero packets={len(wheels)}, '
                  f'max wheel gap during burst={max_gap:.3f}s, stale/range rejected')
        except Exception:
            log.seek(0); print(log.read())
            raise
        finally:
            node.destroy_node(); rclpy.shutdown()
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            state['run'] = False; thread.join(timeout=1)
            os.close(slave); os.close(master)


if __name__ == '__main__':
    main()
