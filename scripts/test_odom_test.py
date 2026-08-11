"""Минимальные тесты чистой математики анализатора."""

import argparse
import contextlib
import importlib.util
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("odom_test.py")
SPEC = importlib.util.spec_from_file_location("odom_test", SCRIPT)
assert SPEC and SPEC.loader
ODOM_TEST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ODOM_TEST
SPEC.loader.exec_module(ODOM_TEST)


class OdomMathTest(unittest.TestCase):
    """Проверки граничной математики без ROS."""

    def test_speed_profile_returns_to_start_with_bounded_excursion(self):
        """Автопрофиль должен вернуться и не выйти за двухметровый участок."""
        peak, finish = ODOM_TEST.profile_peak_displacement(ODOM_TEST.SPEED_PROFILE)
        self.assertAlmostEqual(peak, 1.95)
        self.assertAlmostEqual(finish, 0.0)

    def test_angle_delta_unwraps_both_directions(self):
        """Yaw должен разворачиваться через границу ±pi в обе стороны."""
        self.assertAlmostEqual(
            ODOM_TEST.angle_delta([math.radians(350), math.radians(10)]),
            math.radians(20),
        )
        self.assertAlmostEqual(
            ODOM_TEST.angle_delta([math.radians(10), math.radians(350)]),
            math.radians(-20),
        )

    def test_restart_sessions_split_only_on_large_gap(self):
        """Большой разрыв должен отделять новую сессию odom."""
        sample = ODOM_TEST.Sample
        sessions = ODOM_TEST.odom_sessions(
            [
                sample(0.00, (0.0,)),
                sample(0.02, (0.0,)),
                sample(1.00, (0.0,)),
                sample(1.02, (0.0,)),
            ]
        )
        self.assertEqual([len(session) for session in sessions], [2, 2])

    def test_endpoint_delta_uses_initial_robot_heading(self):
        """Смещение odom должно проецироваться в исходную систему робота."""
        sample = ODOM_TEST.Sample
        data = {
            "joints": [sample(0.0, (1.0, 2.0)), sample(1.0, (3.0, 5.0))],
            "odom": [
                sample(0.0, (1.0, 2.0, math.pi / 2.0, 0.0, 0.0)),
                sample(1.0, (1.0, 4.0, math.pi / 2.0, 0.0, 0.0)),
            ],
        }
        delta = ODOM_TEST.endpoint_delta(data)
        self.assertAlmostEqual(delta["left_joint_rad"], 2.0)
        self.assertAlmostEqual(delta["right_joint_rad"], 3.0)
        self.assertAlmostEqual(delta["odom_forward_m"], 2.0)
        self.assertAlmostEqual(delta["odom_lateral_m"], 0.0)

    def test_encoder_cadence_ignores_stationary_lead_in(self):
        """Ожидание до старта не должно выглядеть потерей кадров."""
        sample = ODOM_TEST.Sample
        integrity = ODOM_TEST.encoder_integrity(
            [
                sample(0.0, (0.0, 0.0)),
                sample(2.0, (0.0, 0.0)),
                sample(2.2, (0.1, 0.1)),
                sample(2.4, (0.2, 0.2)),
                sample(2.6, (0.3, 0.3)),
            ],
            [],
        )
        self.assertTrue(integrity["pass"])
        self.assertAlmostEqual(integrity["max_update_gap_s"], 0.2)

    def test_complete_synthetic_report_passes(self):
        """Полная согласованная синтетическая серия должна получить PASS."""
        runs = [
            {
                "kind": "straight",
                "direction": direction,
                "status": "PASS",
                "pass": True,
                "scale_ratio_reported_over_actual": ratio,
                "speed_tracking_ratio": 1.01,
                "estimated_speed_coefficient": 0.000171,
            }
            for direction, ratio in (
                ("forward", 1.01),
                ("forward", 1.00),
                ("reverse", 0.99),
                ("reverse", 1.00),
            )
        ]
        runs.extend(
            {
                "kind": "spin",
                "direction": direction,
                "status": "PASS",
                "pass": True,
                "yaw_ratio_reported_over_actual": ratio,
            }
            for direction, ratio in (("cw", 1.01), ("ccw", 0.99))
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, run in enumerate(runs):
                run_dir = root / str(index)
                run_dir.mkdir()
                (run_dir / "result.json").write_text(json.dumps(run), encoding="utf-8")
            args = argparse.Namespace(paths=[root], output=root / "summary")
            with contextlib.redirect_stdout(io.StringIO()):
                return_code = ODOM_TEST.report(args)
            summary = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(return_code, 0)
        self.assertEqual(summary["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
