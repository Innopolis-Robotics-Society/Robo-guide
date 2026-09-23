"""Потоковая озвучка: `dialog.streaming` и её связка с `run_turn()` на фейках."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from guide_robot_llm.dialog.streaming import SentenceStreamer, extract_reply_text
from guide_robot_llm.dialog.turn import ToolCallRecord, run_turn
from guide_robot_llm.llm_client import CompletionResult
from guide_robot_llm.llm_client.errors import BackendAborted


@dataclass
class _FakeResult:
    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class _FakeStream:
    """Записывает, что и в каком порядке ушло бы в tts_node."""

    events: list[tuple[str, str]] = field(default_factory=list)
    result: _FakeResult = field(default_factory=_FakeResult)

    def say_first(self, text: str) -> None:
        self.events.append(("first", text))

    def push(self, text: str) -> None:
        self.events.append(("push", text))

    def close(self, tail: str) -> _FakeResult:
        self.events.append(("close", tail))
        return self.result

    def cancel(self) -> None:
        self.events.append(("cancel", ""))


def _feed(streamer: SentenceStreamer, text: str, step: int = 3) -> None:
    for index in range(0, len(text), step):
        streamer.on_delta(text[index : index + step])


# -- extract_reply_text -------------------------------------------------------


def test_extract_waits_for_reply_tool() -> None:
    assert extract_reply_text('{"think": "x", "args": {"text": "При') is None
    assert extract_reply_text('{"tool": "guide_to", "args": {"text": "При') is None
    assert extract_reply_text('{"tool": "reply", "args": {') is None


def test_extract_decodes_partial_string_and_escapes() -> None:
    raw = '{"tool": "reply", "args": {"text": "Он сказал \\"да\\".\\nИ ушёл \\u2014 молча'
    assert extract_reply_text(raw) == 'Он сказал "да".\nИ ушёл — молча'


def test_extract_stops_on_incomplete_escape() -> None:
    assert extract_reply_text('{"tool":"reply","args":{"text":"Раз\\') == "Раз"
    assert extract_reply_text('{"tool":"reply","args":{"text":"Раз\\u20') == "Раз"


def test_extract_stops_at_closing_quote() -> None:
    raw = '{"tool":"reply","args":{"text":"Готово."}}'
    assert extract_reply_text(raw) == "Готово."


def test_extract_matches_json_loads_on_full_object() -> None:
    text = 'Лидар — «глаза» робота.\tОн "видит" 360°.'
    raw = json.dumps({"think": "t", "tool": "reply", "args": {"text": text}}, ensure_ascii=False)
    assert extract_reply_text(raw) == text


# -- SentenceStreamer ---------------------------------------------------------


def test_first_sentence_opens_goal_rest_is_pushed_tail_closes() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    text = "Лидар измеряет расстояния. Он стоит на крыше робота. А сонары внизу"
    _feed(streamer, text)
    spoken, result = streamer.finish(text)
    assert stream.events == [
        ("first", "Лидар измеряет расстояния."),
        ("push", "Он стоит на крыше робота."),
        ("close", "А сонары внизу"),
    ]
    assert spoken == text
    assert result.ok


def test_no_split_on_abbreviation_or_lowercase() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Это музей им. Попова, т. е. про радио")
    assert not streamer.started


def test_no_boundary_until_next_sentence_starts() -> None:
    """Точка в хвосте -- ещё не граница: следующий токен может быть «5» в «2.5»."""
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Высота робота 1.")
    assert not streamer.started
    _feed(streamer, "5 метра. Вес")
    assert stream.events == [("first", "Высота робота 1.5 метра.")]


def test_markdown_is_sanitized_per_chunk() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Робот: **Привет**, я гид. Пойдём")
    assert stream.events == [("first", "Привет, я гид.")]


def test_json_leak_freezes_stream() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    text = 'Сейчас отведу. {"tool": "guide_to", "args": {}} Идём. Быстро'
    _feed(streamer, text)
    spoken, _ = streamer.finish(text)
    assert spoken == "Сейчас отведу."
    assert ("push", "Идём.") not in stream.events


def test_budget_caps_total_length() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream, max_chars=40)
    text = "Первое предложение тут. Второе предложение длинное очень. Третье. Ещё"
    _feed(streamer, text)
    spoken, _ = streamer.finish(text)
    assert len(spoken) <= 40
    assert spoken.startswith("Первое предложение тут.")


def test_aborted_before_first_sound_speaks_nothing() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream, check_aborted=lambda: True)
    _feed(streamer, "Первое. Второе. Третье")
    assert stream.events == []
    assert not streamer.started


def test_abort_after_start_cancels_goal() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Первое предложение. Второе")
    streamer.abort()
    assert stream.events[-1] == ("cancel", "")


def test_restart_before_start_discards_partial_text() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Обрывок ответа без конца")
    streamer.restart()
    _feed(streamer, "Новый ответ. Целиком")
    assert stream.events == [("first", "Новый ответ.")]


def test_restart_after_start_keeps_only_spoken() -> None:
    stream = _FakeStream()
    streamer = SentenceStreamer(stream)
    _feed(streamer, "Первый ответ. Его продолжение")
    streamer.restart()
    _feed(streamer, "Второй ответ. Другой")
    spoken, _ = streamer.finish()
    assert spoken == "Первый ответ."
    assert stream.events == [("first", "Первый ответ."), ("close", "")]


# -- run_turn -----------------------------------------------------------------


def _streaming_action(raw: str, *, step: int = 4):
    """Фейк фазы действия: гонит `raw` дельтами, как SSE."""
    calls: list[dict] = []

    def _complete_action(messages, grammar, **kwargs) -> CompletionResult:
        del messages, grammar
        calls.append(kwargs)
        if kwargs.get("on_attempt"):
            kwargs["on_attempt"]()
        on_delta = kwargs.get("on_delta")
        for index in range(0, len(raw), step):
            if on_delta:
                on_delta(raw[index : index + step])
        return CompletionResult(text=raw)

    _complete_action.calls = calls  # type: ignore[attr-defined]
    return _complete_action


def _run(stream: _FakeStream, complete_action, **overrides):
    kwargs = {
        "system_prompt": "sys",
        "history_messages": [],
        "user_content": "user",
        "complete_answer": lambda messages, **_: CompletionResult(text="Ответ фазы реплики."),
        "complete_action": complete_action,
        "speak": lambda text: pytest.fail(f"обычный speak при стриминге: {text!r}"),
        "execute_tool": lambda name, args: _FakeResult(ok=True),
        "tool_names": ["guide_to", "reply"],
        "action_instruction": "A",
        "answer_instruction": "B",
        "inline_reply": True,
        "open_speech_stream": lambda: stream,
    }
    kwargs.update(overrides)
    return run_turn(**kwargs)


def test_inline_reply_streams_before_json_closes() -> None:
    text = "Лидар стоит сверху. Он крутится. Сонары снизу."
    raw = json.dumps({"think": "t", "tool": "reply", "args": {"text": text}}, ensure_ascii=False)
    resolved: list[ToolCallRecord] = []
    stream = _FakeStream()
    stream_events_at_resolve: list[int] = []

    def _on_resolved(record: ToolCallRecord) -> None:
        resolved.append(record)
        stream_events_at_resolve.append(len(stream.events))

    result = _run(stream, _streaming_action(raw), on_action_resolved=_on_resolved)

    assert stream.events == [
        ("first", "Лидар стоит сверху."),
        ("push", "Он крутится."),
        ("close", "Сонары снизу."),
    ]
    assert result.answer_text == text
    assert result.answer_source == "inline"
    assert result.say_ok
    assert result.action is not None and result.action.name == "reply"
    assert result.action.think == "t"
    # Следствия хода закоммичены ДО первого звука и ровно один раз.
    assert len(resolved) == 1
    assert stream_events_at_resolve == [0]


def test_single_sentence_reply_falls_back_to_plain_speak() -> None:
    raw = json.dumps({"tool": "reply", "args": {"text": "Привет!"}}, ensure_ascii=False)
    spoken: list[str] = []
    stream = _FakeStream()
    result = _run(
        stream,
        _streaming_action(raw),
        speak=lambda text: spoken.append(text) or _FakeResult(ok=True),
    )
    assert stream.events == []
    assert spoken == ["Привет!"]
    assert result.answer_text == "Привет!"


def test_start_tour_override_is_not_streamed() -> None:
    raw = json.dumps(
        {"tool": "reply", "args": {"text": "Конечно. Начинаем экскурсию. Идём"}},
        ensure_ascii=False,
    )
    stream = _FakeStream()
    _run(
        stream,
        _streaming_action(raw),
        tool_names=["start_tour", "reply"],
        utterance="начни экскурсию",
        default_tour_id="main",
        speak=lambda text: _FakeResult(ok=True),
    )
    assert stream.events == []


def test_abort_mid_stream_cancels_goal() -> None:
    text = "Первое предложение. Второе предложение. Третье"
    raw = json.dumps({"tool": "reply", "args": {"text": text}}, ensure_ascii=False)
    stream = _FakeStream()

    def _complete_action(messages, grammar, **kwargs) -> CompletionResult:
        del messages, grammar
        kwargs["on_delta"](raw[: raw.index("Второе") + 8])
        raise BackendAborted("barge-in")

    with pytest.raises(BackendAborted):
        _run(stream, _complete_action)
    assert stream.events == [("first", "Первое предложение."), ("cancel", "")]


def test_answer_phase_streams_after_tool() -> None:
    stream = _FakeStream()
    raw = json.dumps({"tool": "guide_to", "args": {"location_id": "cafe"}})
    answer = "Веду вас к кафе. Идите за мной. Там вкусно"

    def _complete_answer(messages, **kwargs) -> CompletionResult:
        del messages
        for index in range(0, len(answer), 5):
            kwargs["on_delta"](answer[index : index + 5])
        return CompletionResult(text=answer)

    result = _run(
        stream,
        _streaming_action(raw),
        complete_answer=_complete_answer,
        utterance="отведи в кафе",
    )
    assert stream.events == [
        ("first", "Веду вас к кафе."),
        ("push", "Идите за мной."),
        ("close", "Там вкусно"),
    ]
    assert result.answer_text == answer
    assert result.action is not None and result.action.name == "guide_to"
