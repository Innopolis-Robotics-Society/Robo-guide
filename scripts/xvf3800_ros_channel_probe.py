#!/usr/bin/env python3
"""Измерить оба USB capture-канала XVF3800 из диагностического ROS-топика."""

from __future__ import annotations

import argparse
import math
import time
import wave
from pathlib import Path

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

from guide_robot_msgs.msg import AudioChunk

QOS_AUDIO = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=20,
    reliability=ReliabilityPolicy.BEST_EFFORT,
)


def _dbfs(value: float) -> float:
    return -120.0 if value <= 0.0 else 20.0 * math.log10(value / 32768.0)


def analyse_interleaved(samples: np.ndarray) -> list[dict[str, float | int]]:
    """Посчитать уровень, пик и клиппинг каждого столбца int16 PCM."""
    if samples.ndim != 2 or samples.shape[1] < 1:
        raise ValueError("ожидается матрица frames × channels")
    result: list[dict[str, float | int]] = []
    for index in range(samples.shape[1]):
        channel = samples[:, index].astype(np.int32)
        absolute = np.abs(channel)
        rms = float(np.sqrt(np.mean(channel.astype(np.float64) ** 2)))
        peak = int(np.max(absolute, initial=0))
        result.append(
            {
                "channel": index,
                "rms_dbfs": _dbfs(rms),
                "peak_dbfs": _dbfs(float(peak)),
                "clipped_samples": int(np.count_nonzero(absolute >= 32767)),
            }
        )
    return result


class ChannelProbe(Node):
    """Короткоживущий подписчик `/audio/mic_stereo`."""

    def __init__(self, topic: str) -> None:
        """Подписаться на диагностический stereo-топик."""
        super().__init__("xvf3800_ros_channel_probe")
        self.blocks: list[np.ndarray] = []
        self.sample_rate = 0
        self.channels = 0
        self.expected_first_sample: int | None = None
        self.gaps = 0
        self.create_subscription(AudioChunk, topic, self._on_audio, QOS_AUDIO)

    def _on_audio(self, message: AudioChunk) -> None:
        if message.channels < 1 or len(message.data) % message.channels:
            self.get_logger().error("получен повреждённый AudioChunk")
            return
        if self.sample_rate and (
            message.sample_rate != self.sample_rate or message.channels != self.channels
        ):
            self.get_logger().error("формат /audio/mic_stereo изменился во время измерения")
            return
        self.sample_rate = int(message.sample_rate)
        self.channels = int(message.channels)
        if (
            self.expected_first_sample is not None
            and message.first_sample != self.expected_first_sample
        ):
            self.gaps += 1
        frames = len(message.data) // message.channels
        self.expected_first_sample = int(message.first_sample) + frames
        block = np.asarray(message.data, dtype=np.int16).reshape(frames, message.channels)
        self.blocks.append(block)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(samples.shape[1])
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())


def main() -> int:
    """Собрать заданный интервал PCM, вывести метрики и при необходимости WAV."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="/audio/mic_stereo")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--record", type=Path)
    arguments = parser.parse_args()
    if arguments.seconds <= 0:
        parser.error("--seconds должен быть > 0")

    rclpy.init()
    node = ChannelProbe(arguments.topic)
    deadline = time.monotonic() + arguments.seconds
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()

    if not node.blocks:
        print(f"нет данных из {arguments.topic}; запустите launch с publish_stereo_debug:=true")
        return 2

    samples = np.concatenate(node.blocks)
    duration = samples.shape[0] / node.sample_rate
    print(
        f"получено {samples.shape[0]} кадров, {node.channels} каналов, "
        f"{node.sample_rate} Гц, {duration:.2f} с, разрывов first_sample: {node.gaps}"
    )
    for metrics in analyse_interleaved(samples):
        print(
            f"channel {metrics['channel']}: RMS {metrics['rms_dbfs']:.1f} dBFS, "
            f"peak {metrics['peak_dbfs']:.1f} dBFS, "
            f"clipped {metrics['clipped_samples']}"
        )
    if arguments.record is not None:
        _write_wav(arguments.record, samples, node.sample_rate)
        print(f"записано: {arguments.record}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
