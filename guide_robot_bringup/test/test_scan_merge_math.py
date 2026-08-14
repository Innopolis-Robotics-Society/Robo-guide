#!/usr/bin/env python3
"""Unit tests for scan_merge_math (pure numpy, no ROS needed).

Run from the package dir:  cd guide_robot_bringup && python3 -m pytest test -q
"""

import math

import numpy as np
import pytest

from guide_robot_bringup import scan_merge_math as smm


def test_polar_to_points_drops_invalid_and_out_of_range():
    ranges = [1.0, float("inf"), float("nan"), 0.05, 13.0, 2.0]
    pts = smm.polar_to_points(ranges, 0.0, math.pi / 2, 0.1, 12.0)
    assert pts.shape == (2, 3)
    # ray 0: (1, 0); ray 5: 2 m at 5*pi/2 -> (0, 2)
    np.testing.assert_allclose(pts[0], [1.0, 0.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(pts[1], [0.0, 2.0, 0.0], atol=1e-9)


def test_make_transform_rotation_and_translation():
    t = smm.make_transform(
        (1.0, 2.0, 3.0), (0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4))
    )
    pts = np.array([[1.0, 0.0, 0.0]])
    out = smm.apply_transform(pts, t)
    np.testing.assert_allclose(out[0], [1.0, 3.0, 3.0], atol=1e-12)


def test_make_transform_rejects_degenerate_quaternion_safely():
    t = smm.make_transform((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0))
    np.testing.assert_allclose(t, np.eye(4), atol=1e-12)


def test_planar_transform_matches_yaw_offset():
    t = smm.make_planar_transform(1.0, 0.0, math.pi / 2)
    out = smm.apply_transform(np.array([[1.0, 0.0, 0.0]]), t)
    np.testing.assert_allclose(out[0], [1.0, 1.0, 0.0], atol=1e-12)


def _odom_base(x, y, yaw):
    return smm.make_transform((x, y, 0.0), (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)))


def test_deskew_delta_compensates_rotation():
    # Robot turns CCW by theta between src and dst: a world point fixed ahead
    # must rotate CW by theta in the base frame.
    theta = 0.1
    delta = smm.deskew_delta(_odom_base(0.0, 0.0, 0.0), _odom_base(0.0, 0.0, theta))
    out = smm.apply_transform(np.array([[1.0, 0.0, 0.0]]), delta)
    np.testing.assert_allclose(out[0], [math.cos(-theta), math.sin(-theta), 0.0], atol=1e-12)


def test_deskew_delta_compensates_translation():
    delta = smm.deskew_delta(_odom_base(0.5, 0.0, 0.0), _odom_base(0.0, 0.0, 0.0))
    out = smm.apply_transform(np.array([[0.0, 0.0, 0.0]]), delta)
    np.testing.assert_allclose(out[0], [0.5, 0.0, 0.0], atol=1e-12)


def test_bin_points_nearest_wins_and_fill():
    pts = np.array([[2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.5, 0.001, 0.0]])
    ranges = smm.bin_points(pts, -math.pi, math.pi, 0.005, 0.1, 12.0, float("inf"))
    center = int(math.ceil(2 * math.pi / 0.005)) // 2
    assert ranges[center] == pytest.approx(1.0)
    # Everything else untouched (inf fill).
    assert np.isinf(ranges[center + 10])


def test_bin_points_empty_input():
    ranges = smm.bin_points(np.zeros((0, 3)), -math.pi, math.pi, 0.005, 0.1, 12.0, 13.0)
    assert ranges.shape == (int(math.ceil(2 * math.pi / 0.005)),)
    assert np.all(ranges == 13.0)


def test_bin_points_respects_angle_window():
    pts = np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]])  # angles 0 and pi
    ranges = smm.bin_points(pts, -0.5, 0.5, 0.005, 0.1, 12.0, float("inf"))
    center = int(math.ceil(1.0 / 0.005)) // 2
    assert ranges[center] == pytest.approx(1.0)
    assert np.isinf(ranges[0])  # pi is outside the window


def test_two_lidars_rotating_robot_land_in_same_bin():
    """Synthetic regression for the old inter-scan desync.

    A wall point sits at (5, 0) in odom. The robot spins CCW at omega; the
    left lidar scans it at t0 (yaw 0), the right one d_t later (yaw
    omega*d_t). Without deskew the two returns disagree by omega*d_t; with
    deskew to a common instant both must land in the same angular bin.
    """
    omega, d_t = 1.0, 0.042
    point_odom = np.array([[5.0, 0.0, 0.0]])

    def scan_point_at(yaw):
        # base->odom at that instant; the lidar measures p in its own base frame
        t_o_b = _odom_base(0.0, 0.0, yaw)
        return smm.apply_transform(point_odom, np.linalg.inv(t_o_b)), t_o_b

    p0, t0 = scan_point_at(0.0)
    p1, t1 = scan_point_at(omega * d_t)

    # Raw angular disagreement (what the old merger published).
    raw_err = abs(math.atan2(p1[0, 1], p1[0, 0]) - math.atan2(p0[0, 1], p0[0, 0]))
    assert raw_err == pytest.approx(omega * d_t, rel=1e-9)

    t_common = _odom_base(0.0, 0.0, omega * d_t / 2)
    p0_c = smm.apply_transform(p0, smm.deskew_delta(t0, t_common))
    p1_c = smm.apply_transform(p1, smm.deskew_delta(t1, t_common))
    np.testing.assert_allclose(p0_c[0], p1_c[0], atol=1e-9)

    increment = 0.005
    binned = smm.bin_points(
        np.vstack((p0_c, p1_c)), -math.pi, math.pi, increment, 0.1, 12.0, float("inf")
    )
    assert np.sum(np.isfinite(binned)) == 1  # one bin, not two
