"""Юниты на конвертацию yaw -> кватернион."""

from __future__ import annotations

import math

import pytest

from guide_robot_semantic_map.lib.geometry import path_length_m, yaw_to_quaternion_xyzw


def test_zero_yaw_is_identity() -> None:
    assert yaw_to_quaternion_xyzw(0.0) == (0.0, 0.0, 0.0, 1.0)


def test_half_turn() -> None:
    x, y, z, w = yaw_to_quaternion_xyzw(math.pi)
    assert (x, y) == (0.0, 0.0)
    assert z == pytest.approx(1.0)
    assert w == pytest.approx(0.0, abs=1e-9)


def test_quarter_turn() -> None:
    x, y, z, w = yaw_to_quaternion_xyzw(math.pi / 2)
    assert (x, y) == (0.0, 0.0)
    assert z == pytest.approx(math.sqrt(2) / 2)
    assert w == pytest.approx(math.sqrt(2) / 2)


def test_negative_yaw_flips_z_sign() -> None:
    _, _, z, w = yaw_to_quaternion_xyzw(-math.pi / 2)
    assert z < 0
    assert w > 0


@pytest.mark.parametrize("yaw", [0.0, 0.3, -0.3, math.pi / 4, -math.pi / 4, 2.5, -2.5])
def test_round_trips_through_atan2(yaw: float) -> None:
    _, _, z, w = yaw_to_quaternion_xyzw(yaw)
    recovered = 2.0 * math.atan2(z, w)
    assert recovered == pytest.approx(yaw)


def test_x_and_y_always_zero() -> None:
    for yaw in (0.0, 1.0, -1.0, 10.0):
        x, y, _, _ = yaw_to_quaternion_xyzw(yaw)
        assert (x, y) == (0.0, 0.0)


def test_unit_quaternion() -> None:
    for yaw in (0.0, 0.5, 1.5, -1.5, 3.0):
        x, y, z, w = yaw_to_quaternion_xyzw(yaw)
        assert x * x + y * y + z * z + w * w == pytest.approx(1.0)


# -- path_length_m (design.md §1.2) ---------------------------------------------


def test_path_length_empty() -> None:
    assert path_length_m([]) == 0.0


def test_path_length_single_point() -> None:
    assert path_length_m([(1.0, 2.0)]) == 0.0


def test_path_length_straight_line() -> None:
    points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    assert path_length_m(points) == pytest.approx(2.0)


def test_path_length_sums_segments() -> None:
    # 3-4-5 треугольник в двух отрезках.
    points = [(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)]
    assert path_length_m(points) == pytest.approx(3.0 + 4.0)


def test_path_length_dense_path_close_to_straight() -> None:
    # Плотный путь с мелким шагом -- сумма отрезков близка к прямой.
    points = [(x / 10.0, 0.0) for x in range(101)]
    assert path_length_m(points) == pytest.approx(10.0)
