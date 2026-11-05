"""Bounded correction for a position command, independent of the serial port."""

import statistics
from collections import deque


def compensated_target(target, actual, raw_min, raw_max, bias=25):
    """Apply a small bounded one-shot bias in the direction of travel."""
    error = target - actual
    offset = bias if error > 30 else -bias if error < -30 else 0
    return max(raw_min, min(raw_max, target + offset))


def can_retarget(side, last_side, elapsed, interval):
    """Rate-limit changes on the same side; allow a reversal immediately."""
    return not last_side or side != last_side or elapsed >= interval


class HeadFeedback:
    """Summarize recent, separately received encoder observations."""

    def __init__(self):
        self.samples = deque(maxlen=8)
        self.target = None

    def sample(self, now, raw):
        # Repeated packets in one USB burst are not independent samples.
        if self.samples and now - self.samples[-1][0] < 0.12:
            return
        self.samples.append((now, raw))

    def position(self, now):
        recent = [(t, v) for t, v in self.samples if now - t <= 1.5]
        if len(recent) < 4 or now - recent[-1][0] > 0.7:
            return None
        if recent[-1][0] - recent[0][0] < 0.6:
            return None
        values = [v for _, v in recent[-5:]]
        return statistics.median(values), max(values) - min(values)

    def command(self, target):
        self.target = target
