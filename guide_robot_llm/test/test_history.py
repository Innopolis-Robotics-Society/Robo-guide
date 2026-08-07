"""Юниты на историю диалога."""

from __future__ import annotations

from guide_robot_llm.lib.history import History


def test_window_is_empty_initially() -> None:
    history = History(max_history_turns=3)
    assert history.window() == []
    assert len(history) == 0


def test_append_turn_appears_in_window_as_user_then_assistant() -> None:
    history = History(max_history_turns=3)
    history.append_turn("привет", "привет, чем помочь?", interrupted=False)
    assert history.window() == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "привет, чем помочь?"},
    ]


def test_window_uses_spoken_text_not_generated() -> None:
    """В историю попадает произнесённое, а не то, что сгенерировала модель."""
    history = History(max_history_turns=3)
    history.append_turn("расскажи про экспонат", "этот экспонат из", interrupted=True)
    window = history.window()
    assert window[1]["content"].startswith("этот экспонат из")


def test_interrupted_turn_gets_marker_suffix() -> None:
    history = History(max_history_turns=3, interrupted_marker=" [прервано]")
    history.append_turn("вопрос", "начало ответа", interrupted=True)
    assert window_assistant(history) == "начало ответа [прервано]"


def test_non_interrupted_turn_has_no_marker() -> None:
    history = History(max_history_turns=3, interrupted_marker=" [прервано]")
    history.append_turn("вопрос", "полный ответ", interrupted=False)
    assert window_assistant(history) == "полный ответ"


def test_window_evicts_oldest_turn_beyond_max_history_turns() -> None:
    history = History(max_history_turns=2)
    history.append_turn("вопрос1", "ответ1", interrupted=False)
    history.append_turn("вопрос2", "ответ2", interrupted=False)
    history.append_turn("вопрос3", "ответ3", interrupted=False)
    window = history.window()
    users = [m["content"] for m in window if m["role"] == "user"]
    assert users == ["вопрос2", "вопрос3"]
    assert len(history) == 2


def test_len_tracks_number_of_turns() -> None:
    history = History(max_history_turns=5)
    assert len(history) == 0
    history.append_turn("a", "b", interrupted=False)
    assert len(history) == 1


def window_assistant(history: History) -> str:
    return next(m["content"] for m in history.window() if m["role"] == "assistant")
