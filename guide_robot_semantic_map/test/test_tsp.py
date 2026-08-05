"""Юниты на открытый TSP с фиксированным стартом."""

from __future__ import annotations

import itertools
import random

import pytest

from guide_robot_semantic_map.lib.tsp import TspError, path_cost, solve_open_path


def _brute_force(start_costs: list[float], costs: list[list[float]]) -> tuple[list[int], float]:
    n = len(start_costs)
    best_order: list[int] = []
    best_cost = float("inf")
    for perm in itertools.permutations(range(n)):
        cost = path_cost(list(perm), start_costs, costs)
        if cost < best_cost:
            best_cost, best_order = cost, list(perm)
    return best_order, best_cost


def _random_instance(n: int, *, seed: int) -> tuple[list[float], list[list[float]]]:
    rng = random.Random(seed)
    start_costs = [rng.uniform(1.0, 20.0) for _ in range(n)]
    costs = [[0.0 if i == j else rng.uniform(1.0, 20.0) for j in range(n)] for i in range(n)]
    return start_costs, costs


# -- path_cost ------------------------------------------------------------------


def test_path_cost_empty_order_is_zero() -> None:
    assert path_cost([], [1.0], [[0.0]]) == 0.0


def test_path_cost_single_stop_is_start_cost() -> None:
    assert path_cost([0], [3.5], [[0.0]]) == pytest.approx(3.5)


def test_path_cost_sums_start_and_transitions() -> None:
    start_costs = [1.0, 2.0]
    costs = [[0.0, 5.0], [7.0, 0.0]]
    assert path_cost([0, 1], start_costs, costs) == pytest.approx(1.0 + 5.0)
    assert path_cost([1, 0], start_costs, costs) == pytest.approx(2.0 + 7.0)


# -- solve_open_path: краевые случаи ---------------------------------------------


def test_solve_empty_instance() -> None:
    result = solve_open_path([], [])
    assert result.order == []
    assert result.total_cost == 0.0
    assert result.exact is True


def test_solve_single_point() -> None:
    result = solve_open_path([4.2], [[0.0]])
    assert result.order == [0]
    assert result.total_cost == pytest.approx(4.2)
    assert result.exact is True


def test_rejects_mismatched_costs_shape() -> None:
    with pytest.raises(TspError):
        solve_open_path([1.0, 2.0], [[0.0, 1.0]])


def test_rejects_non_square_costs() -> None:
    with pytest.raises(TspError):
        solve_open_path([1.0, 2.0], [[0.0, 1.0, 2.0], [1.0, 0.0, 2.0]])


# -- Held-Karp против брутфорса (design.md §5) ------------------------------------


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_held_karp_matches_brute_force(n: int) -> None:
    start_costs, costs = _random_instance(n, seed=100 + n)
    result = solve_open_path(start_costs, costs)
    assert result.exact is True
    _, brute_cost = _brute_force(start_costs, costs)
    assert result.total_cost == pytest.approx(brute_cost)
    assert path_cost(result.order, start_costs, costs) == pytest.approx(result.total_cost)
    assert sorted(result.order) == list(range(n))


def test_held_karp_respects_asymmetric_costs() -> None:
    # 0 -> 1 дёшево (1.0), 1 -> 0 дорого (100.0) -- решение обязано
    # использовать направление, а не считать costs[i][j] == costs[j][i].
    start_costs = [1.0, 50.0]
    costs = [[0.0, 1.0], [100.0, 0.0]]
    result = solve_open_path(start_costs, costs)
    assert result.order == [0, 1]
    assert result.total_cost == pytest.approx(2.0)


def test_held_karp_used_up_to_exact_limit() -> None:
    start_costs, costs = _random_instance(12, seed=1)
    result = solve_open_path(start_costs, costs)
    assert result.exact is True


# -- эвристика для N > 12 ---------------------------------------------------------


def test_heuristic_used_above_exact_limit() -> None:
    start_costs, costs = _random_instance(13, seed=2)
    result = solve_open_path(start_costs, costs)
    assert result.exact is False
    assert sorted(result.order) == list(range(13))


def test_heuristic_beats_or_matches_nearest_neighbour_baseline() -> None:
    start_costs, costs = _random_instance(20, seed=3)
    result = solve_open_path(start_costs, costs)

    # naive nearest-neighbour без локального поиска, как нижняя граница качества.
    n = len(start_costs)
    unvisited = set(range(n))
    nn_order: list[int] = []
    current = start_costs
    for _ in range(n):
        nxt = min(unvisited, key=lambda k: current[k])
        nn_order.append(nxt)
        unvisited.discard(nxt)
        current = costs[nxt]
    nn_cost = path_cost(nn_order, start_costs, costs)

    assert result.total_cost <= nn_cost + 1e-9


def test_heuristic_result_is_valid_permutation() -> None:
    start_costs, costs = _random_instance(25, seed=4)
    result = solve_open_path(start_costs, costs)
    assert sorted(result.order) == list(range(25))
    assert path_cost(result.order, start_costs, costs) == pytest.approx(result.total_cost)


def test_heuristic_respects_time_budget() -> None:
    start_costs, costs = _random_instance(20, seed=5)
    result = solve_open_path(start_costs, costs, time_budget_ms=1.0)
    # С бюджетом почти в 0 мс всё равно должен вернуть валидную перестановку.
    assert sorted(result.order) == list(range(20))
    assert result.exact is False


def test_heuristic_deterministic_for_same_input() -> None:
    start_costs, costs = _random_instance(15, seed=6)
    first = solve_open_path(start_costs, costs)
    second = solve_open_path(start_costs, costs)
    assert first.order == second.order
    assert first.total_cost == pytest.approx(second.total_cost)
