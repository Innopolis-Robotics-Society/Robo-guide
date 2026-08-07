"""Оценка длительности тура по метрам пути и dwell (design.md §1.2).

duration_min = (Sum(leg_dist_m) / v_eff + Sum(dwell_s) + n_stops * turn_penalty_s) / 60
v_eff = nominal_speed_mps * crowd_factor

Параметры по умолчанию (nominal_speed_mps=0.35, crowd_factor=0.7,
turn_penalty_s=3.0) намеренно пессимистичны -- музей с толпой, а не
паспортная скорость робота. Недооценка длительности ломает
time_budget_min в RunTour, переоценка -- нет, поэтому асимметрия в
дефолтах в сторону "медленнее" осознанная.

distance_m -- сумма евклидовых отрезков nav_msgs/Path (плотный путь),
а не route_cost (design.md §1.2 -- route_cost это скор для TSP, не метры).
Эта функция работает только с метрами и секундами, откуда взялась
дистанция -- забота вызывающего кода (route_planner).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["EstimateParams", "estimate_duration_min"]


@dataclass(frozen=True)
class EstimateParams:
    """Параметры оценки длительности тура."""

    nominal_speed_mps: float = 0.35
    crowd_factor: float = 0.7
    turn_penalty_s: float = 3.0

    @property
    def v_eff(self) -> float:
        """Эффективная скорость с поправкой на толпу, м/с."""
        return self.nominal_speed_mps * self.crowd_factor


def estimate_duration_min(
    leg_distances_m: Sequence[float],
    dwell_s: Sequence[float],
    *,
    params: EstimateParams | None = None,
) -> float:
    """Оценить длительность тура в минутах.

    leg_distances_m[i] и dwell_s[i] относятся к одной и той же остановке:
    расстояние до неё от предыдущей точки (старта или предыдущей
    остановки) и время задержки на ней. n_stops в формуле -- их общая
    длина, поэтому обе последовательности обязаны совпадать по размеру.
    """
    if len(leg_distances_m) != len(dwell_s):
        raise ValueError(
            "leg_distances_m и dwell_s должны быть одной длины "
            f"(по одной паре на остановку), получено {len(leg_distances_m)} и {len(dwell_s)}"
        )

    cfg = params or EstimateParams()
    if cfg.v_eff <= 0.0:
        raise ValueError("v_eff должен быть положительным (nominal_speed_mps * crowd_factor)")

    n_stops = len(dwell_s)
    travel_s = sum(leg_distances_m) / cfg.v_eff
    dwell_total_s = sum(dwell_s)
    turn_penalty_total_s = n_stops * cfg.turn_penalty_s
    total_s = travel_s + dwell_total_s + turn_penalty_total_s
    return total_s / 60.0
