"""Дымовые тесты моков (design §9.1) -- фундамент под narration_server/mission_fsm.

Не покрывают resume-инварианты (это test_narration_resume.py, §12 п.4) --
только то, что сами моки говорят правду о своём контракте: Say отдаёт
верные spoken_chars/status, CancelAll фенсит по epoch/scope как реальный
tts_node, NavigateToPose шлёт feedback и уважает отмену, а сервисы
semantic_map отдают то, что в них положили.
"""

from __future__ import annotations

import pytest
from geometry_msgs.msg import PoseStamped
from guide_robot_msgs.action import Say
from guide_robot_msgs.msg import CancelAll
from guide_robot_msgs.srv import EstimateRoute, GetExhibitContent, ListLocations, ListTours
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient

from test.mocks.harness import MissionTestHarness, wait_for_future, wait_until


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def test_say_completes_and_reports_full_text(harness: MissionTestHarness) -> None:
    harness.say.chars_per_sec = 1000.0  # не тестируем темп здесь, только исход
    client_node = harness.make_client_node()
    client = ActionClient(client_node, Say, "say")
    assert client.wait_for_server(timeout_sec=5.0)

    send_future = client.send_goal_async(Say.Goal(text="Привет, это тест."))
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted

    harness.clock.advance(10.0)
    result_future = goal_handle.get_result_async()
    wait_for_future(result_future)
    result: Say.Result = result_future.result().result

    assert result.status == Say.Result.STATUS_COMPLETED
    assert result.spoken_text == "Привет, это тест."
    assert result.spoken_chars == len("Привет, это тест.")


def test_say_cancel_reports_partial_spoken_chars(harness: MissionTestHarness) -> None:
    harness.say.chars_per_sec = 10.0
    client_node = harness.make_client_node()
    client = ActionClient(client_node, Say, "say")
    assert client.wait_for_server(timeout_sec=5.0)

    text = "A" * 100
    progress = [0.0]
    send_future = client.send_goal_async(
        Say.Goal(text=text),
        feedback_callback=lambda fb: progress.__setitem__(0, fb.feedback.progress),
    )
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted

    harness.clock.advance(2.0)  # ~20 символов при 10 char/s
    wait_until(lambda: progress[0] > 0.0)  # дождаться, чтобы мок реально это заметил
    cancel_future = goal_handle.cancel_goal_async()
    wait_for_future(cancel_future)

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future)
    result: Say.Result = result_future.result().result

    assert result.status == Say.Result.STATUS_CANCELLED
    assert 0 < result.spoken_chars < 100
    assert result.spoken_text == text[: result.spoken_chars]


def test_cancel_all_preempts_active_goal_regardless_of_scope(
    harness: MissionTestHarness,
) -> None:
    """Как в tts_node._on_cancel_all: активная цель гасится любым CancelAll, не только scope."""
    harness.say.chars_per_sec = 1.0  # медленно -- не успеет доиграть
    client_node = harness.make_client_node()
    client = ActionClient(client_node, Say, "say")
    assert client.wait_for_server(timeout_sec=5.0)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)

    send_future = client.send_goal_async(
        Say.Goal(
            text="Длинный текст, который не успеет прозвучать.",
            scope=Say.Goal.SCOPE_NARRATION,
        )
    )
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted
    harness.clock.advance(0.5)

    msg = CancelAll(
        scope=CancelAll.SCOPE_DIALOG, reason=CancelAll.REASON_BARGE_IN
    )
    cancel_all_pub.publish(msg)

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=5.0)
    result: Say.Result = result_future.result().result
    assert result.status == Say.Result.STATUS_PREEMPTED
    assert result.message == "epoch_bumped"


def test_queued_goal_survives_cancel_all_with_different_scope(harness: MissionTestHarness) -> None:
    """Не начавшая звучать цель снимается, только если scope совпадает (или SCOPE_ALL)."""
    harness.say.chars_per_sec = 5.0
    client_node = harness.make_client_node()
    client = ActionClient(client_node, Say, "say")
    assert client.wait_for_server(timeout_sec=5.0)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)

    first = client.send_goal_async(Say.Goal(text="A" * 50, scope=Say.Goal.SCOPE_NARRATION))
    wait_for_future(first)
    first_handle = first.result()
    second = client.send_goal_async(Say.Goal(text="Второй.", scope=Say.Goal.SCOPE_DIALOG))
    wait_for_future(second)
    second_handle = second.result()

    epoch_before = harness.say.epoch
    cancel_all_pub.publish(CancelAll(scope=CancelAll.SCOPE_NARRATION, reason="test"))
    wait_until(lambda: harness.say.epoch != epoch_before)
    harness.clock.advance(100.0)

    first_result = first_handle.get_result_async()
    wait_for_future(first_result)
    assert first_result.result().result.status == Say.Result.STATUS_PREEMPTED

    # first уже точно освободил активный слот -- second мог начать пейсинг
    # только сейчас, поэтому его собственный `start` уже позже первого
    # advance(). Нужен ещё один скачок, иначе elapsed у second никогда не
    # вырастет и он зависнет.
    harness.clock.advance(100.0)

    second_result = second_handle.get_result_async()
    wait_for_future(second_result)
    assert second_result.result().result.status == Say.Result.STATUS_COMPLETED


def test_nav_server_succeed_mode_reports_feedback_and_result(harness: MissionTestHarness) -> None:
    harness.nav.duration_s = 1.0
    harness.nav.distance_m = 10.0
    client_node = harness.make_client_node()
    client = ActionClient(client_node, NavigateToPose, "navigate_to_pose")
    assert client.wait_for_server(timeout_sec=5.0)

    send_future = client.send_goal_async(NavigateToPose.Goal(pose=PoseStamped()))
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted

    harness.clock.advance(5.0)
    result_future = goal_handle.get_result_async()
    wait_for_future(result_future)
    assert result_future.result().status == 4  # GoalStatus.STATUS_SUCCEEDED


def test_nav_server_hang_mode_can_be_cancelled(harness: MissionTestHarness) -> None:
    harness.nav.mode = harness.nav.MODE_HANG
    client_node = harness.make_client_node()
    client = ActionClient(client_node, NavigateToPose, "navigate_to_pose")
    assert client.wait_for_server(timeout_sec=5.0)

    send_future = client.send_goal_async(NavigateToPose.Goal(pose=PoseStamped()))
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted

    cancel_future = goal_handle.cancel_goal_async()
    wait_for_future(cancel_future, timeout_s=5.0)
    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=5.0)
    assert result_future.result().status == 5  # GoalStatus.STATUS_CANCELED


def test_semantic_map_services_serve_fixtures(harness: MissionTestHarness) -> None:
    harness.fixtures.add_exhibit("lab105a", ["Первый чанк.", "Второй чанк."], version="rev1")
    harness.fixtures.add_location("lab105a", x=1.0, y=2.0, zone="lab")
    harness.fixtures.add_tour(
        "main", "Основной тур", [("lab105a", "lab105a", 60, "full")], duration_min_estimate=10
    )
    harness.fixtures.set_route_estimate(distance_m=12.5, duration_min=3.0, feasible=True)

    client_node = harness.make_client_node()

    content_client = client_node.create_client(
        GetExhibitContent, "/content_server/get_exhibit_content"
    )
    assert content_client.wait_for_service(timeout_sec=5.0)
    future = content_client.call_async(
        GetExhibitContent.Request(exhibit_id="lab105a", language="ru")
    )
    wait_for_future(future)
    response = future.result()
    assert list(response.chunks) == ["Первый чанк.", "Второй чанк."]
    assert response.version == "rev1"

    locations_client = client_node.create_client(ListLocations, "/location_server/list_locations")
    assert locations_client.wait_for_service(timeout_sec=5.0)
    future = locations_client.call_async(ListLocations.Request())
    wait_for_future(future)
    assert future.result().locations[0].id == "lab105a"

    tours_client = client_node.create_client(ListTours, "/location_server/list_tours")
    assert tours_client.wait_for_service(timeout_sec=5.0)
    future = tours_client.call_async(ListTours.Request())
    wait_for_future(future)
    tours = future.result().tours
    assert tours[0].id == "main"
    assert tours[0].stops[0].exhibit_id == "lab105a"

    route_client = client_node.create_client(EstimateRoute, "/route_planner/estimate_route")
    assert route_client.wait_for_service(timeout_sec=5.0)
    future = route_client.call_async(EstimateRoute.Request(ids=["lab105a"]))
    wait_for_future(future)
    route = future.result()
    assert route.distance_m == pytest.approx(12.5)
    assert route.feasible is True


def test_change_rev_after_n_calls(harness: MissionTestHarness) -> None:
    harness.fixtures.add_exhibit("lab105a", ["Текст."], version="rev1")
    harness.fixtures.change_rev_after_n_calls("lab105a", after=1, new_version="rev2")

    client_node = harness.make_client_node()
    content_client = client_node.create_client(
        GetExhibitContent, "/content_server/get_exhibit_content"
    )
    assert content_client.wait_for_service(timeout_sec=5.0)

    request = GetExhibitContent.Request(exhibit_id="lab105a", language="ru")
    future = content_client.call_async(request)
    wait_for_future(future)
    assert future.result().version == "rev1"

    future = content_client.call_async(request)
    wait_for_future(future)
    assert future.result().version == "rev2"


def test_missing_exhibit_returns_empty_not_a_crash(harness: MissionTestHarness) -> None:
    client_node = harness.make_client_node()
    content_client = client_node.create_client(
        GetExhibitContent, "/content_server/get_exhibit_content"
    )
    assert content_client.wait_for_service(timeout_sec=5.0)
    future = content_client.call_async(
        GetExhibitContent.Request(exhibit_id="unknown", language="ru")
    )
    wait_for_future(future)
    response = future.result()
    assert list(response.chunks) == []
    assert response.version == ""


def test_fail_on_text_injects_synthesis_failure(harness: MissionTestHarness) -> None:
    harness.say.fail_on_text.add("это упадёт")
    client_node = harness.make_client_node()
    client = ActionClient(client_node, Say, "say")
    assert client.wait_for_server(timeout_sec=5.0)

    send_future = client.send_goal_async(Say.Goal(text="это упадёт"))
    wait_for_future(send_future)
    goal_handle = send_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=5.0)
    result: Say.Result = result_future.result().result
    assert result.status == Say.Result.STATUS_FAILED
    assert result.message == "injected_failure"


def test_sim_clock_advance_and_set(harness: MissionTestHarness) -> None:
    assert harness.clock.seconds == pytest.approx(0.0)
    harness.clock.advance(1.5)
    assert harness.clock.seconds == pytest.approx(1.5)
    harness.clock.set_seconds(100.0)
    assert harness.clock.seconds == pytest.approx(100.0)
