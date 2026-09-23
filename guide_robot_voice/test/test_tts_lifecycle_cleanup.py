"""Регрессия lifecycle-cleanup интерфейсов tts_node."""

from guide_robot_voice.tts_node import TtsNode


class _Destroyable:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def destroy(self) -> None:
        self._events.append(f"destroy:{self._name}")


class _Closable:
    def __init__(self, events: list[str], name: str) -> None:
        self._events = events
        self._name = name

    def close(self) -> None:
        self._events.append(f"close:{self._name}")


def test_release_resources_destroys_ros_interfaces_and_is_idempotent() -> None:
    events: list[str] = []
    node = TtsNode.__new__(TtsNode)

    node._status_timer = object()
    node._action_server = _Destroyable(events, "action")
    node._cancel_sub = object()
    node._status_pub = object()
    node._diag_pub = object()
    node._event_pub = object()
    node._sink = _Closable(events, "sink")
    node._backend = _Closable(events, "backend")

    node.destroy_timer = lambda timer: events.append("destroy:timer")
    node.destroy_subscription = lambda subscription: events.append("destroy:subscription")
    node.destroy_lifecycle_publisher = lambda publisher: events.append("destroy:publisher")

    node._release_resources()
    node._release_resources()

    assert events == [
        "destroy:timer",
        "destroy:action",
        "destroy:subscription",
        "destroy:publisher",
        "destroy:publisher",
        "destroy:publisher",
        "close:sink",
        "close:backend",
    ]
    assert node._status_timer is None
    assert node._action_server is None
    assert node._cancel_sub is None
    assert node._status_pub is None
    assert node._diag_pub is None
    assert node._event_pub is None
    assert node._sink is None
    assert node._backend is None
