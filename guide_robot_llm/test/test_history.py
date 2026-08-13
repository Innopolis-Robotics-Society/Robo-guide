"""`dialog.history.DialogHistory` -- чистая логика, без ROS (DIALOG_REWORK_PLAN.md §3.1)."""

from __future__ import annotations

from guide_robot_llm.dialog.history import DialogHistory


def test_visitor_and_robot_roles_render_in_order() -> None:
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)
    history.add_robot("привет, я робот", ts=2.0)

    assert history.to_messages() == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "привет, я робот"},
    ]


def test_cap_applied_at_write_time_on_word_boundary() -> None:
    history = DialogHistory(cap_visitor=10)
    history.add_visitor("раз два три четыре пять", ts=1.0)

    messages = history.to_messages()
    assert len(messages[0]["content"]) <= 11  # cap + многоточие
    assert messages[0]["content"].endswith("…")
    assert " " not in messages[0]["content"][-2:]  # обрезано по границе слова


def test_cap_not_applied_when_text_within_limit() -> None:
    history = DialogHistory(cap_visitor=200)
    history.add_visitor("коротко", ts=1.0)

    assert history.to_messages()[0]["content"] == "коротко"


def test_truncated_robot_reply_gets_suffix() -> None:
    history = DialogHistory()
    history.add_robot("незакончен", ts=1.0, truncated=True)

    assert history.to_messages()[0]["content"] == "незакончен …(реплика была прервана)"


def test_non_truncated_robot_reply_has_no_suffix() -> None:
    history = DialogHistory()
    history.add_robot("закончен", ts=1.0, truncated=False)

    assert history.to_messages()[0]["content"] == "закончен"


def test_consecutive_events_merge_into_one_message() -> None:
    history = DialogHistory()
    history.add_event("перешёл в NARRATING", ts=1.0)
    history.add_event("подошёл к остановке lab_demo", ts=2.0)

    messages = history.to_messages()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert (
        messages[0]["content"]
        == "СОБЫТИЕ: перешёл в NARRATING\nСОБЫТИЕ: подошёл к остановке lab_demo"
    )


def test_event_between_visitor_turns_does_not_merge_across_them() -> None:
    history = DialogHistory()
    history.add_visitor("а что тут?", ts=1.0)
    history.add_event("подошёл к остановке lab_demo", ts=2.0)
    history.add_robot("это макет кампуса", ts=3.0)

    messages = history.to_messages()
    assert [m["role"] for m in messages] == ["user", "user", "assistant"]
    assert messages[1]["content"] == "СОБЫТИЕ: подошёл к остановке lab_demo"


def test_render_holds_trailing_events() -> None:
    """Хвостовые события не сбрасываются в отдельное сообщение -- уходят списком."""
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)
    history.add_robot("здравствуйте", ts=2.0)
    history.add_event("перешёл в NAVIGATING", ts=3.0)
    history.add_event("подошёл к остановке «вход»", ts=4.0)

    messages, trailing = history.render()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert trailing == ["перешёл в NAVIGATING", "подошёл к остановке «вход»"]


def test_render_flushes_mid_history_events() -> None:
    """События МЕЖДУ репликами рендерятся обычным СОБЫТИЕ:-сообщением."""
    history = DialogHistory()
    history.add_visitor("а что тут?", ts=1.0)
    history.add_event("подошёл к остановке lab_demo", ts=2.0)
    history.add_robot("это макет кампуса", ts=3.0)

    messages, trailing = history.render()
    assert [m["role"] for m in messages] == ["user", "user", "assistant"]
    assert messages[1]["content"] == "СОБЫТИЕ: подошёл к остановке lab_demo"
    assert trailing == []


def test_render_does_not_mutate_state() -> None:
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)
    history.add_event("перешёл в NAVIGATING", ts=2.0)

    first = history.render()
    second = history.render()
    assert first == second
    assert len(history) == 2


def test_to_messages_flushes_trailing_events_like_before() -> None:
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)
    history.add_event("перешёл в NAVIGATING", ts=2.0)

    assert history.to_messages() == [
        {"role": "user", "content": "привет"},
        {"role": "user", "content": "СОБЫТИЕ: перешёл в NAVIGATING"},
    ]


def test_trimming_happens_in_halves_not_one_at_a_time() -> None:
    history = DialogHistory(max_entries=4, trim_to=2)
    for i in range(4):
        history.add_visitor(f"msg{i}", ts=float(i))
    assert len(history) == 4

    history.add_visitor("msg4", ts=4.0)  # 5-я запись -- превышение

    assert len(history) == 2
    contents = [m["content"] for m in history.to_messages()]
    assert contents == ["msg3", "msg4"]


def test_trimming_only_triggers_on_exceeding_max() -> None:
    history = DialogHistory(max_entries=4, trim_to=2)
    for i in range(4):
        history.add_visitor(f"msg{i}", ts=float(i))

    assert len(history) == 4  # ровно на пределе -- обрезки ещё не было


def test_events_do_not_count_toward_max_entries() -> None:
    """События не съедают лимит реплик -- живой баг: память диалога сжималась
    до 4-5 ходов, потому что переходы FSM считались наравне с репликами."""
    history = DialogHistory(max_entries=4, trim_to=2)
    for i in range(4):
        history.add_visitor(f"msg{i}", ts=float(i))
        history.add_event(f"event{i}", ts=float(i))

    # 4 реплики + 4 события: реплик ровно на пределе -- обрезки нет.
    assert len(history) == 8


def test_trimming_keeps_events_between_kept_replies() -> None:
    history = DialogHistory(max_entries=4, trim_to=2)
    for i in range(4):
        history.add_visitor(f"msg{i}", ts=float(i))
        history.add_event(f"event{i}", ts=float(i))

    history.add_visitor("msg4", ts=4.0)  # 5-я реплика -- превышение

    # Остаются 2 последние реплики (msg3, msg4) и событие МЕЖДУ ними;
    # события до msg3 уходят вместе с обрезанными репликами.
    contents = [m["content"] for m in history.to_messages()]
    assert contents == ["msg3", "СОБЫТИЕ: event3", "msg4"]


def test_clear_is_idempotent() -> None:
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)
    history.clear()
    history.clear()

    assert len(history) == 0
    assert history.to_messages() == []


def test_to_messages_does_not_mutate_state() -> None:
    history = DialogHistory()
    history.add_visitor("привет", ts=1.0)

    history.to_messages()
    history.to_messages()

    assert len(history) == 1
