from __future__ import annotations

import numpy as np

from scripts.xvf3800_ros_channel_probe import analyse_interleaved


def test_analyse_interleaved_keeps_channels_separate() -> None:
    samples = np.array(
        [
            [0, 1000],
            [32767, -1000],
            [-32768, 1000],
            [0, -1000],
        ],
        dtype=np.int16,
    )

    metrics = analyse_interleaved(samples)

    assert len(metrics) == 2
    assert metrics[0]["channel"] == 0
    assert metrics[0]["clipped_samples"] == 2
    assert metrics[0]["peak_dbfs"] == 0.0
    assert metrics[1]["channel"] == 1
    assert metrics[1]["clipped_samples"] == 0
    assert metrics[1]["peak_dbfs"] < -30.0
