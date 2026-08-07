"""Открытый TSP с фиксированным стартом, без возврата (design.md §1.2).

Старт -- текущая поза робота, не входит в набор точек и не имеет
индекса в costs; costs[i][j] -- стоимость перехода i -> j между уже
известными точками, start_costs[i] -- стоимость первого перехода
старт -> i. Матрица направленная (граф Route Server ориентированный,
турникеты и односторонние галереи -- обычное дело), поэтому costs[i][j]
может не равняться costs[j][i], и локальный поиск ниже пересчитывает
стоимость пути целиком, а не дельтой по паре рёбер, как в симметричном
2-opt.

N <= 12: точный Held-Karp по подмножествам (2**12 * 12**2 ~ 6*10**5
операций, единицы мс). N > 12: nearest-neighbour + 2-opt + or-opt до
сходимости или бюджета времени -- обе эвристики гарантированно сходятся,
потому что каждое принятое улучшение строго уменьшает суммарную
стоимость, а число перестановок конечно.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["TspError", "TspResult", "path_cost", "solve_open_path"]

_EXACT_LIMIT = 12
_EPS = 1e-9


class TspError(ValueError):
    """Некорректные входные данные для TSP: размер costs не совпадает со start_costs."""


@dataclass(frozen=True)
class TspResult:
    """Решение открытого TSP."""

    order: list[int]
    total_cost: float
    exact: bool
    """True -- Held-Karp (гарантированно оптимально), False -- эвристика."""


def path_cost(
    order: Sequence[int], start_costs: Sequence[float], costs: Sequence[Sequence[float]]
) -> float:
    """Стоимость пути старт -> order[0] -> order[1] -> ... -> order[-1].

    Используется и внутри solve_open_path, и напрямую вызывающим кодом
    для optimize=false (design.md §1.2: "порядок как пришёл, только оценка").
    """
    if not order:
        return 0.0
    total = start_costs[order[0]]
    for a, b in pairwise(order):
        total += costs[a][b]
    return total


def solve_open_path(
    start_costs: Sequence[float],
    costs: Sequence[Sequence[float]],
    *,
    time_budget_ms: float | None = None,
) -> TspResult:
    """Найти порядок обхода, минимизирующий path_cost."""
    n = len(start_costs)
    _validate(start_costs, costs)
    if n == 0:
        return TspResult(order=[], total_cost=0.0, exact=True)
    if n == 1:
        return TspResult(order=[0], total_cost=start_costs[0], exact=True)
    if n <= _EXACT_LIMIT:
        return _held_karp(start_costs, costs)
    return _heuristic(start_costs, costs, time_budget_ms)


def _validate(start_costs: Sequence[float], costs: Sequence[Sequence[float]]) -> None:
    n = len(start_costs)
    if len(costs) != n or any(len(row) != n for row in costs):
        raise TspError(f"costs должна быть {n}x{n} по числу точек в start_costs ({n})")


# -- точное решение: Held-Karp -------------------------------------------------


def _held_karp(start_costs: Sequence[float], costs: Sequence[Sequence[float]]) -> TspResult:
    n = len(start_costs)
    size = 1 << n
    inf = float("inf")
    dp = [[inf] * n for _ in range(size)]
    parent = [[-1] * n for _ in range(size)]

    for j in range(n):
        dp[1 << j][j] = start_costs[j]

    for mask in range(size):
        for j in range(n):
            if not (mask & (1 << j)) or dp[mask][j] == inf:
                continue
            current = dp[mask][j]
            for k in range(n):
                if mask & (1 << k):
                    continue
                new_mask = mask | (1 << k)
                new_cost = current + costs[j][k]
                if new_cost < dp[new_mask][k]:
                    dp[new_mask][k] = new_cost
                    parent[new_mask][k] = j

    full = size - 1
    best_j = min(range(n), key=lambda j: dp[full][j])
    best_cost = dp[full][best_j]

    order: list[int] = []
    mask, j = full, best_j
    while j != -1:
        order.append(j)
        prev_j = parent[mask][j]
        mask ^= 1 << j
        j = prev_j
    order.reverse()

    return TspResult(order=order, total_cost=best_cost, exact=True)


# -- эвристика: nearest-neighbour + 2-opt + or-opt ------------------------------


def _nearest_neighbour(
    start_costs: Sequence[float], costs: Sequence[Sequence[float]]
) -> list[int]:
    n = len(start_costs)
    unvisited = set(range(n))
    order: list[int] = []
    current_costs = start_costs
    for _ in range(n):
        nxt = min(unvisited, key=lambda k: current_costs[k])
        order.append(nxt)
        unvisited.discard(nxt)
        current_costs = costs[nxt]
    return order


def _deadline_passed(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _two_opt_pass(
    order: list[int],
    start_costs: Sequence[float],
    costs: Sequence[Sequence[float]],
    deadline: float | None,
) -> bool:
    """Развернуть сегмент [i, j], если это уменьшает суммарную стоимость.

    "Первое улучшение" с перезапуском скана после каждого принятого хода:
    асимметричные costs не допускают дешёвой дельты по двум рёбрам, как
    в симметричном 2-opt, поэтому кандидат каждый раз пересчитывается
    целиком -- пересчёт после разворота сегмента дороже, чем локальная
    дельта, зато корректен для направленного графа.
    """
    any_improved = False
    improved = True
    while improved:
        improved = False
        current_cost = path_cost(order, start_costs, costs)
        n = len(order)
        for i in range(n - 1):
            for j in range(i + 1, n):
                if _deadline_passed(deadline):
                    return any_improved
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if path_cost(candidate, start_costs, costs) < current_cost - _EPS:
                    order[:] = candidate
                    any_improved = improved = True
                    break
            if improved:
                break
    return any_improved


def _or_opt_pass(
    order: list[int],
    start_costs: Sequence[float],
    costs: Sequence[Sequence[float]],
    deadline: float | None,
) -> bool:
    """Перенести цепочку из 1-3 точек в другое место пути, если это выгодно."""
    any_improved = False
    improved = True
    while improved:
        improved = False
        current_cost = path_cost(order, start_costs, costs)
        n = len(order)
        for seg_len in (1, 2, 3):
            if seg_len >= n:
                continue
            for i in range(n - seg_len + 1):
                segment = order[i : i + seg_len]
                remainder = order[:i] + order[i + seg_len :]
                for insert_at in range(len(remainder) + 1):
                    if _deadline_passed(deadline):
                        return any_improved
                    candidate = remainder[:insert_at] + segment + remainder[insert_at:]
                    if candidate == order:
                        continue
                    if path_cost(candidate, start_costs, costs) < current_cost - _EPS:
                        order[:] = candidate
                        any_improved = improved = True
                        break
                if improved:
                    break
            if improved:
                break
    return any_improved


def _heuristic(
    start_costs: Sequence[float],
    costs: Sequence[Sequence[float]],
    time_budget_ms: float | None,
) -> TspResult:
    order = _nearest_neighbour(start_costs, costs)
    deadline = None if time_budget_ms is None else time.monotonic() + time_budget_ms / 1000.0

    while not _deadline_passed(deadline):
        improved_two_opt = _two_opt_pass(order, start_costs, costs, deadline)
        if _deadline_passed(deadline):
            break
        improved_or_opt = _or_opt_pass(order, start_costs, costs, deadline)
        if not improved_two_opt and not improved_or_opt:
            break

    return TspResult(order=order, total_cost=path_cost(order, start_costs, costs), exact=False)
