"""Юниты на оценку длительности тура."""

from __future__ import annotations

import pytest

from guide_robot_semantic_map.lib.estimate import EstimateParams, estimate_duration_min


def test_default_params_match_design() -> None:
    params = EstimateParams()
    assert params.nominal_speed_mps == pytest.approx(0.35)
    assert params.crowd_factor == pytest.approx(0.7)
    assert params.turn_penalty_s == pytest.approx(3.0)


def test_v_eff_is_speed_times_crowd_factor() -> None:
    params = EstimateParams(nominal_speed_mps=0.5, crowd_factor=0.8)
    assert params.v_eff == pytest.approx(0.4)


def test_zero_stops_gives_zero_duration() -> None:
    assert estimate_duration_min([], []) == pytest.approx(0.0)


def test_hand_computed_single_stop() -> None:
    params = EstimateParams(nominal_speed_mps=1.0, crowd_factor=1.0, turn_penalty_s=0.0)
    # 10 м при v_eff=1 м/с -> 10 с хода + 20 с dwell = 30 с = 0.5 мин.
    duration = estimate_duration_min([10.0], [20.0], params=params)
    assert duration == pytest.approx(0.5)


def test_turn_penalty_applied_per_stop() -> None:
    params = EstimateParams(nominal_speed_mps=1.0, crowd_factor=1.0, turn_penalty_s=6.0)
    # Без хода и dwell, только штраф за повороты: 2 остановки * 6 с = 12 с = 0.2 мин.
    duration = estimate_duration_min([0.0, 0.0], [0.0, 0.0], params=params)
    assert duration == pytest.approx(12.0 / 60.0)


def test_default_params_are_pessimistic() -> None:
    # v_eff по умолчанию (0.35*0.7=0.245 м/с) должен быть медленнее
    # nominal_speed_mps -- crowd_factor обязан снижать скорость, а не
    # завышать её (design.md §1.2: "оценка должна быть пессимистичной").
    assert EstimateParams().v_eff < EstimateParams().nominal_speed_mps


def test_matches_design_formula_directly() -> None:
    params = EstimateParams()
    leg_distances = [5.0, 3.0, 8.0]
    dwell = [45.0, 60.0, 120.0]
    expected_s = (
        sum(leg_distances) / params.v_eff + sum(dwell) + len(dwell) * params.turn_penalty_s
    )
    assert estimate_duration_min(leg_distances, dwell) == pytest.approx(expected_s / 60.0)


def test_mismatched_lengths_raise() -> None:
    with pytest.raises(ValueError, match="одной длины"):
        estimate_duration_min([1.0, 2.0], [1.0])


def test_zero_v_eff_raises() -> None:
    params = EstimateParams(nominal_speed_mps=0.0)
    with pytest.raises(ValueError, match="v_eff"):
        estimate_duration_min([1.0], [1.0], params=params)


def test_negative_crowd_factor_raises() -> None:
    params = EstimateParams(crowd_factor=-1.0)
    with pytest.raises(ValueError, match="v_eff"):
        estimate_duration_min([1.0], [1.0], params=params)
