"""Сброс костмап Nav2 в начале каждого RunTour."""

from __future__ import annotations

import pytest
from guide_robot_msgs.action import RunTour
from nav2_msgs.srv import ClearEntireCostmap

from test.mission_fsm_test_helpers import (
    make_fsm_node,
    make_narration_node,
    make_run_tour_client,
    pump_clock,
    setup_single_stop_tour,
)
from test.mocks.harness import MissionTestHarness, wait_for_future

_SERVICES = ["/test/global_costmap/clear", "/test/local_costmap/clear"]


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _run_single_stop_tour(harness: MissionTestHarness, client_node) -> RunTour.Result:
    _, run_tour_client = make_run_tour_client(harness)
    goal_future = run_tour_client.send_goal_async(
        RunTour.Goal(
            location_ids=["lab105a"],
            greet=False,
            narrate=False,
            confirm_between_stops=False,
            return_home=False,
        )
    )
    wait_for_future(goal_future)
    assert goal_future.result().accepted
    result_future = goal_future.result().get_result_async()
    pump_clock(harness, result_future.done, step=0.1, max_iterations=200)
    wait_for_future(result_future, timeout_s=15.0)
    return result_future.result().result


def test_costmaps_cleared_before_first_navigation(harness: MissionTestHarness) -> None:
    setup_single_stop_tour(harness)
    make_narration_node(harness)
    make_fsm_node(harness, clear_costmaps_on_tour_start=True, clear_costmap_services=_SERVICES)
    client_node = harness.make_client_node("costmap_mock")
    # Сколько nav-goal-ов было к моменту каждого сброса.
    calls: list[tuple[str, int]] = []

    def _handler(name: str):
        def _cb(request, response):
            calls.append((name, harness.nav.goals_received))
            return response

        return _cb

    for name in _SERVICES:
        client_node.create_service(ClearEntireCostmap, name, _handler(name))

    result = _run_single_stop_tour(harness, client_node)

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert sorted(name for name, _ in calls) == sorted(_SERVICES)
    assert all(nav_goals == 0 for _, nav_goals in calls)


def test_missing_costmap_service_does_not_block_tour(harness: MissionTestHarness) -> None:
    setup_single_stop_tour(harness)
    make_narration_node(harness)
    make_fsm_node(
        harness,
        clear_costmaps_on_tour_start=True,
        clear_costmap_services=_SERVICES,
        clear_costmaps_timeout_s=0.2,
    )
    client_node = harness.make_client_node("costmap_mock")

    result = _run_single_stop_tour(harness, client_node)

    assert result.outcome == RunTour.Result.OUTCOME_COMPLETED
    assert harness.nav.goals_received == 1
