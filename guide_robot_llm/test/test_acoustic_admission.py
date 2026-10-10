"""Содержательная допущенная команда не теряется из-за timeout текстового wake."""

from types import SimpleNamespace

import pytest
from builtin_interfaces.msg import Time

from guide_robot_llm.dialog_agent_node import DialogAgentNode
from guide_robot_msgs.msg import Transcript


@pytest.mark.parametrize(
    "text,admitted,expected",
    [
        ("расскажи о картине", True, ["расскажи о картине"]),
        ("расскажи о картине", False, []),
        ("расскажи о феррари и фирая", True, ["расскажи о феррари и фирая"]),
        ("феррари это машина", True, ["феррари это машина"]),
        ("расскажи фирая об экскурсии", True, ["расскажи фирая об экскурсии"]),
        ("ферая расскажи о картине", True, ["расскажи о картине"]),
    ],
)
def test_acoustic_admission_survives_expired_textual_listen_window(text, admitted, expected):
    handled = []
    fake = SimpleNamespace(
        _active=True,
        _listen_armed=lambda: False,
        _wake_grace_active=lambda: False,
        last_mission_state=lambda: object(),
        _pending_confirm_ready=lambda _text: False,
        _disarm_listen=lambda: None,
        _disarm_wake_grace=lambda: None,
        _handle_transcript=lambda text, **_kwargs: handled.append(text),
        get_logger=lambda: SimpleNamespace(info=lambda _text: None),
    )
    msg = Transcript(text=text, is_final=True, wakeword_admitted=admitted)
    DialogAgentNode._on_transcript(fake, msg)
    assert handled == expected


@pytest.mark.parametrize("completed", [True, False])
def test_turn_result_preserves_capture_identity_and_actual_completion(completed):
    events = []
    fake = SimpleNamespace(
        _dialog_turn_pub=SimpleNamespace(publish=events.append),
        _active=True,
        _session_id="llm1",
        get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=123))
        ),
    )
    DialogAgentNode._publish_turn_event(fake, ("capture1", 42, 12345), 7, "llm1", completed)
    assert len(events) == 1
    event = events[0]
    assert (event.device_session_id, event.utterance_id, event.start_sample) == (
        "capture1",
        42,
        12345,
    )
    assert event.reply_completed == completed and event.dialog_session_id == "llm1"


@pytest.mark.parametrize(
    "active,session,origin",
    [
        (False, "llm1", ("capture1", 42, 12345)),
        (True, "old", ("capture1", 42, 12345)),
        (True, "llm1", None),
    ],
)
def test_old_or_inactive_dialog_worker_cannot_rearm_voice(active, session, origin):
    fake = SimpleNamespace(
        _dialog_turn_pub=SimpleNamespace(publish=lambda _msg: pytest.fail("stale event")),
        _active=active,
        _session_id="llm1",
    )
    DialogAgentNode._publish_turn_event(fake, origin, 7, session, True)
