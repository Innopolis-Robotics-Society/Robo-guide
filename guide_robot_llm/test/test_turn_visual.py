"""`dialog.turn.run_turn()` -- визуальный контекст (Taiga #4).

Кадроприкрепление фазы действия/реплики (build_content), observe_then_decide
(наблюдение ПЕРЕД действием, side-channel: malformed/BackendError не рвут ход),
стабильность стабильной инструкции в сообщениях фазы действия.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from guide_robot_llm.dialog.turn import run_turn
from guide_robot_llm.llm_client import CompletionResult
from guide_robot_llm.llm_client.errors import BackendAborted, BackendTimeout
from guide_robot_llm.llm_client.grammar import build_observation_grammar
from guide_robot_llm.visual_context import ObservationRequest

_ACTION_INSTRUCTION = "ACTION_INSTRUCTION_STABLE"
_ANSWER_INSTRUCTION = "ANSWER_INSTRUCTION_STABLE"
_FRAME_A = "data:image/jpeg;base64,QUJD"
_FRAME_B = "data:image/jpeg;base64,RUJD"
_SUFFIX = (
    "[Визуальный контекст]\nРеплика посетителя: «что это?»\n"
    "Кандидаты-экспонаты: нет."
)
_VALID_OBSERVATION = (
    '{"people_count": 1, "exhibit_candidates": [], '
    '"pointing_evidence": "yes", "scene_facts": "человек указывает"}'
)


@dataclass
class _FakeResult:
    ok: bool = True
    message: str = ""
    data: dict = field(default_factory=dict)


def _answer(text: str = "Привет!"):
    def _complete_answer(messages: list[dict]) -> CompletionResult:
        del messages
        return CompletionResult(text=text)

    return _complete_answer


def _capture_answer():
    captured: list[list[dict]] = []

    def _complete_answer(messages: list[dict]) -> CompletionResult:
        captured.append(messages)
        return CompletionResult(text="Привет!")

    return _complete_answer, captured


def _reply() -> str:
    return json.dumps({"tool": "reply", "args": {}, "confidence": 0.9, "abstain": False})


def _guide(location_id: str = "cafe") -> str:
    return json.dumps(
        {
            "tool": "guide_to",
            "args": {"location_id": location_id},
            "confidence": 0.9,
            "abstain": False,
        }
    )


def _observation_request(**overrides) -> ObservationRequest:
    kwargs = dict(
        instruction="OBSERVATION_INSTRUCTION_STABLE",
        context_text=_SUFFIX,
        frames=(_FRAME_A, _FRAME_B),
        grammar=build_observation_grammar([]),
        candidate_ids=frozenset(),
        quality="ok",
        max_chars=400,
    )
    kwargs.update(overrides)
    return ObservationRequest(**kwargs)


def _run(action_json: str, **overrides):
    kwargs = dict(
        system_prompt="sys",
        history_messages=[],
        user_content="user",
        complete_answer=_answer(),
        complete_action=lambda messages, grammar, **_kw: CompletionResult(text=action_json),
        speak=lambda text: _FakeResult(ok=True),
        execute_tool=lambda name, args: _FakeResult(ok=True),
        tool_names=("guide_to", "reply"),
        action_instruction=_ACTION_INSTRUCTION,
        answer_instruction=_ANSWER_INSTRUCTION,
    )
    kwargs.update(overrides)
    return run_turn(**kwargs)


# -- фаза действия без кадров (текстовый ход) -----------------------------------


def test_text_only_action_messages_unchanged() -> None:
    captured: list[list[dict]] = []
    complete_action = lambda messages, grammar, **_kw: (  # noqa: E731
        captured.append(messages), CompletionResult(text=_reply())
    )[-1]

    result = _run(_reply(), complete_action=complete_action)

    assert result.stopped_reason == "ok"
    assert captured[0] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
        {"role": "user", "content": _ACTION_INSTRUCTION},
    ]


def test_visual_suffix_without_frames_is_plain_string_message() -> None:
    captured: list[list[dict]] = []

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        captured.append(messages)
        return CompletionResult(text=_reply())

    _run(_reply(), complete_action=complete_action, visual_suffix=_SUFFIX)

    messages = captured[0]
    assert len(messages) == 4
    assert messages[3] == {"role": "user", "content": _SUFFIX}
    assert isinstance(messages[3]["content"], str)


# -- кадры в фазе действия (direct_action) --------------------------------------


def test_action_frames_attached_after_stable_instruction() -> None:
    captured: list[list[dict]] = []

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        captured.append(messages)
        return CompletionResult(text=_reply())

    _run(
        _reply(),
        complete_action=complete_action,
        visual_suffix=_SUFFIX,
        action_frames=(_FRAME_A, _FRAME_B),
    )

    messages = captured[0]
    assert len(messages) == 4
    assert messages[2] == {"role": "user", "content": _ACTION_INSTRUCTION}
    content = messages[3]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": _SUFFIX}
    assert content[1] == {"type": "image_url", "image_url": {"url": _FRAME_A}}
    assert content[2] == {"type": "image_url", "image_url": {"url": _FRAME_B}}


# -- observe_then_decide: наблюдение ПЕРЕД действием ----------------------------


def test_observe_then_decide_runs_observation_before_action() -> None:
    order: list[str] = []
    observation_messages: list[list[dict]] = []

    def complete_observation(
        messages: list[dict], grammar: str, *, stop_when=None
    ) -> CompletionResult:
        del stop_when
        order.append("observation")
        observation_messages.append(messages)
        return CompletionResult(text=_VALID_OBSERVATION)

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        order.append("action")
        return CompletionResult(text=_reply())

    result = _run(
        _reply(),
        complete_action=complete_action,
        complete_observation=complete_observation,
        observation_request=_observation_request(),
        visual_suffix=_SUFFIX,
        action_frames=(_FRAME_A, _FRAME_B),
    )

    assert order == ["observation", "action"]
    obs = observation_messages[0]
    assert obs[0] == {"role": "system", "content": "sys"}
    assert obs[1] == {"role": "user", "content": "user"}
    assert obs[2] == {"role": "user", "content": "OBSERVATION_INSTRUCTION_STABLE"}
    obs_content = obs[3]["content"]
    assert isinstance(obs_content, list)
    assert obs_content[0]["text"] == _SUFFIX
    assert obs_content[1] == {"type": "image_url", "image_url": {"url": _FRAME_A}}

    assert result.observation_error == ""
    assert result.observation_raw_text == _VALID_OBSERVATION
    assert "людей в кадре: 1" in result.observation_text
    assert "указательный жест: есть" in result.observation_text


def test_observation_text_embedded_into_action_visual_message() -> None:
    captured: list[list[dict]] = []

    def complete_observation(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=_VALID_OBSERVATION)

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        captured.append(messages)
        return CompletionResult(text=_reply())

    _run(
        _reply(),
        complete_action=complete_action,
        complete_observation=complete_observation,
        observation_request=_observation_request(),
        visual_suffix=_SUFFIX,
        action_frames=(_FRAME_A,),
    )

    content = captured[0][3]["content"]
    text_part = content[0]["text"]
    assert text_part.startswith(_SUFFIX)
    assert "[Визуальное наблюдение]" in text_part
    assert "указательный жест: есть" in text_part


# -- наблюдение как side-channel: отказы не рвут ход -----------------------------


def test_observation_malformed_degrades_safely() -> None:
    def complete_observation(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text='{"people_count": "много", "extra": 1}')

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=_reply())

    result = _run(
        _reply(),
        complete_action=complete_action,
        complete_observation=complete_observation,
        observation_request=_observation_request(),
        visual_suffix=_SUFFIX,
        action_frames=(_FRAME_A,),
    )

    assert result.stopped_reason == "ok"
    assert result.observation_error == "malformed"
    assert result.observation_text == ""
    # Фаза действия получила только visual_suffix (наблюдения нет).
    assert result.observation_raw_text.startswith('{"people_count"')


def test_observation_backend_error_degrades_safely() -> None:
    def complete_observation(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        raise BackendTimeout("timeout")

    def complete_action(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        return CompletionResult(text=_reply())

    result = _run(
        _reply(),
        complete_action=complete_action,
        complete_observation=complete_observation,
        observation_request=_observation_request(),
        visual_suffix=_SUFFIX,
        action_frames=(_FRAME_A,),
    )

    assert result.stopped_reason == "ok"
    assert result.observation_error == "backend_error"
    assert result.observation_text == ""


def test_observation_aborted_propagates() -> None:
    def complete_observation(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        del messages, grammar
        raise BackendAborted("barge-in")

    with pytest.raises(BackendAborted):
        _run(
            _reply(),
            complete_observation=complete_observation,
            observation_request=_observation_request(),
            visual_suffix=_SUFFIX,
            action_frames=(_FRAME_A,),
        )


def test_observation_skipped_without_frames_text_only_variant() -> None:
    called: list[str] = []

    def complete_observation(messages: list[dict], grammar: str, **_kw) -> CompletionResult:
        called.append("observation")
        del messages, grammar
        return CompletionResult(text=_VALID_OBSERVATION)

    result = _run(
        _reply(),
        complete_observation=complete_observation,
        observation_request=_observation_request(frames=()),
        visual_suffix=_SUFFIX,
    )

    assert called == []  # без кадров наблюдать нечего
    assert result.stopped_reason == "ok"
    assert result.observation_text == ""


# -- кадры в фазе реплики --------------------------------------------------------


def test_answer_phase_frames_attached_for_non_reply_action() -> None:
    _complete_answer, captured = _capture_answer()

    result = _run(
        _guide(),
        complete_answer=_complete_answer,
        known_location_ids=frozenset({"cafe"}),
        answer_frames=(_FRAME_A,),
        answer_phase_images=True,
    )

    assert result.stopped_reason == "ok"
    assert result.action is not None
    assert result.action.name == "guide_to"
    last = captured[0][-1]
    assert last["role"] == "user"
    content = last["content"]
    assert isinstance(content, list)
    assert content[-1] == {"type": "image_url", "image_url": {"url": _FRAME_A}}


def test_answer_phase_reply_never_gets_frames() -> None:
    _complete_answer, captured = _capture_answer()

    result = _run(
        _reply(),
        complete_answer=_complete_answer,
        answer_frames=(_FRAME_A,),
        answer_phase_images=True,
    )

    assert result.stopped_reason == "ok"
    assert isinstance(captured[0][-1]["content"], str)


def test_answer_phase_images_flag_off_never_gets_frames() -> None:
    _complete_answer, captured = _capture_answer()

    _run(
        _guide(),
        complete_answer=_complete_answer,
        known_location_ids=frozenset({"cafe"}),
        answer_frames=(_FRAME_A,),
        answer_phase_images=False,
    )

    assert isinstance(captured[0][-1]["content"], str)


# -- наследуемое визуальное сообщение в фазе реплики (путь ноды) --------------
#
# Реальная нода передаёт action_frames == answer_frames и visual_suffix --
# визуальное сообщение фазы действия (с кадрами) наследуется в список
# сообщений фазы реплики. Контракт: кадры в фазе реплики видны ТОЛЬКО при
# answer_phase_images=true и action != reply -- иначе из наследуемого
# сообщения image-parts уходят, текст (кандидаты/наблюдение) остаётся.


def _count_images(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            total += sum(1 for part in content if part.get("type") == "image_url")
    return total


def test_inherited_visual_message_strips_frames_for_reply() -> None:
    _complete_answer, captured = _capture_answer()

    _run(
        _reply(),
        complete_answer=_complete_answer,
        action_frames=(_FRAME_A,),
        visual_suffix=_SUFFIX,
        answer_frames=(_FRAME_A,),
        answer_phase_images=True,  # даже при включённом флаге reply без кадров
    )

    assert _count_images(captured[0]) == 0
    assert any(_SUFFIX in str(m.get("content")) for m in captured[0])


def test_inherited_visual_message_strips_frames_when_flag_off() -> None:
    _complete_answer, captured = _capture_answer()

    _run(
        _guide(),
        complete_answer=_complete_answer,
        known_location_ids=frozenset({"cafe"}),
        action_frames=(_FRAME_A,),
        visual_suffix=_SUFFIX,
        answer_frames=(_FRAME_A,),
        answer_phase_images=False,
    )

    assert _count_images(captured[0]) == 0
    assert any(_SUFFIX in str(m.get("content")) for m in captured[0])


def test_inherited_visual_message_keeps_frames_when_contract_allows() -> None:
    _complete_answer, captured = _capture_answer()

    _run(
        _guide(),
        complete_answer=_complete_answer,
        known_location_ids=frozenset({"cafe"}),
        action_frames=(_FRAME_A,),
        visual_suffix=_SUFFIX,
        answer_frames=(_FRAME_A,),
        answer_phase_images=True,
    )

    # Наследуемое визуальное сообщение (1 кадр) + кадр в сообщении реплики.
    assert _count_images(captured[0]) == 2


# -- TurnResult вёзёт поля наблюдения -------------------------------------------


def test_result_carries_observation_fields_when_absent() -> None:
    result = _run(_reply())

    assert result.observation_raw_text == ""
    assert result.observation_text == ""
    assert result.observation_error == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
