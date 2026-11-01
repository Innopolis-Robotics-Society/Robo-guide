"""Exercise correction decisions without touching a robot."""

import unittest

from furo_head_feedback import HeadFeedback, compensated_target, can_retarget


class HeadFeedbackTest(unittest.TestCase):
    def test_retarget_delays_same_side_but_allows_reversal(self):
        self.assertFalse(can_retarget(1, 1, 1.0, 2.5))
        self.assertTrue(can_retarget(1, 1, 2.5, 2.5))
        self.assertTrue(can_retarget(-1, 1, 0.1, 2.5))

    def test_one_shot_bias_is_directional_and_clamped(self):
        self.assertEqual(compensated_target(2080, 1800, 1550, 2360), 2105)
        self.assertEqual(compensated_target(1600, 1800, 1550, 2360), 1575)
        self.assertEqual(compensated_target(1550, 1800, 1550, 2360), 1550)
        self.assertEqual(compensated_target(1800, 1780, 1550, 2360), 1800)

    def samples(self, control, start, values):
        for n, value in enumerate(values):
            control.sample(start + n * 0.2, value)

    def test_feedback_window_rejects_stale_data(self):
        c = HeadFeedback()
        self.samples(c, 2.0, [1945, 1947, 1946, 1948, 1946])
        self.assertEqual(c.position(2.9), (1946, 3))
        self.assertIsNone(c.position(5.0))

    def test_burst_does_not_make_feedback_fresh(self):
        c = HeadFeedback()
        for _ in range(1000):
            c.sample(1.0, 1770)
        self.assertIsNone(c.position(1.0))


if __name__ == "__main__":
    unittest.main()
