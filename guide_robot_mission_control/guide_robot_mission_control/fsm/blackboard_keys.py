"""Типизированный блэкборд верхней SM (design §5.3).

Пишется ТОЛЬКО из потока, исполняющего `RootStateMachine.run_tour()`
(внутри `execute_callback` `RunTour`-сервера) -- колбэки подписок узла
кладут запросы в очереди/`Event` на `FsmContext` (fsm/context.py), а не
трогают блэкборд напрямую; design §5.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guide_robot_mission_control.interrupt_stack import InterruptStack

__all__ = ["Blackboard", "TourPlan"]


@dataclass
class TourPlan:
    """Разрешённый план тура -- design §2.4 `RunTour.Goal` + §0.5 (ListTours.stops).

    `stop_ids`/`exhibit_ids` -- параллельные списки одной длины: `stop_ids`
    идёт в `NavigateToPose` (через `ListLocations`-позу), `exhibit_ids` --
    в `Narrate`. При явном `RunTour.Goal.stop_ids` (клиент сам называет
    остановки, минуя `ListTours`) они считаются совпадающими -- так же,
    как в тестовых фикстурах этого пакета совпадают id локации и id
    экспоната.
    """

    stop_ids: list[str]
    exhibit_ids: list[str]
    index: int = 0
    greet: bool = True
    narrate: bool = True
    confirm_between_stops: bool = True
    return_home: bool = True

    @property
    def current_stop_id(self) -> str:
        """location_id остановки, на которой сейчас находится тур."""
        return self.stop_ids[self.index]

    @property
    def current_exhibit_id(self) -> str:
        """exhibit_id текущей остановки -- аргумент для Narrate.Goal."""
        return self.exhibit_ids[self.index]

    @property
    def has_next_stop(self) -> bool:
        """True, если после текущей остановки в туре есть ещё хотя бы одна."""
        return self.index + 1 < len(self.stop_ids)


@dataclass
class Blackboard:
    """Изменяемое состояние одного прогона тура (одного `RunTour`-goal)."""

    tour: TourPlan
    resume_token: str = ""
    narrate_goal_handle: object | None = None
    nav_goal_handle: object | None = None
    stack: InterruptStack = field(default_factory=InterruptStack)
    last_answer: str = ""
    stops_completed: int = 0
    stops_skipped: int = 0

    # -- заполняется root_sm.py при входе в ANSWERING/HELD/PAUSED (design §5.4) --
    interrupted_from: str = ""

    # -- design §5.3, пока не наполняются в шаге 7 --
    presence: object | None = None
    safety: object | None = None
