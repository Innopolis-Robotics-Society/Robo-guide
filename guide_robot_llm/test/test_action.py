"""Строгий слой контракта действия (ADR-0001 §2/§4/§6).

`parse_action` / `verify_action` -- чистая логика, hypothesis в окружении
нет: property-стайл проверен детерминированным seeded fuzz +
handcrafted-противником (все классы нарушения, перечисленные в ишью #5).
"""

from __future__ import annotations

import json
import random

from guide_robot_llm.tools.validate import (
    REASON_ABSTAIN_FROM_MODEL,
    REASON_ILLEGAL_STATE,
    REASON_INVALID_ARGS,
    REASON_LOW_CONFIDENCE,
    REASON_MALFORMED_OUTPUT,  # noqa: F401  -- код парсера, здесь через None
    REASON_UNKNOWN_ID,
    ActionVerdict,
    parse_action,
    verify_action,
)

_TOOLS = ["reply", "guide_to", "start_tour", "stop_tour"]
_LOCATIONS = frozenset({"cafe", "exit"})
_TOURS = frozenset({"lab_demo"})


def _ok() -> ActionVerdict:
    """Вердикт-контроль: action пропустит broker."""
    verdict = verify_action(
        parse_action(
            '{"tool": "guide_to", "args": {"location_id": "cafe"}, '
            '"confidence": 0.9, "abstain": false}'
        ),
        tools_allowed=_TOOLS,
        known_location_ids=_LOCATIONS,
        known_tour_ids=_TOURS,
    )
    assert verdict.ok is True
    return verdict


def test_valid_action_passes() -> None:
    verdict = _ok()
    assert verdict.reason is None
    assert verdict.tool == "guide_to"
    assert verdict.args == {"location_id": "cafe"}
    assert verdict.confidence == 0.9
    assert verdict.abstain is False


def test_integer_confidence_is_accepted_as_float() -> None:
    parsed = parse_action('{"tool": "reply", "args": {}, "confidence": 1, "abstain": false}')
    assert parsed is not None
    assert parsed.confidence == 1.0


def test_extra_key_is_malformed() -> None:
    raw = '{"tool": "reply", "args": {}, "confidence": 0.9, "abstain": false, "think": "x"}'
    assert parse_action(raw) is None


def test_missing_any_field_is_malformed() -> None:
    for field_name in ("tool", "args", "confidence", "abstain"):
        obj = {"tool": "reply", "args": {}, "confidence": 0.9, "abstain": False}
        del obj[field_name]
        assert parse_action(json.dumps(obj)) is None, field_name


def test_non_object_top_level_is_malformed() -> None:
    assert parse_action("[1, 2]") is None
    assert parse_action('"reply"') is None
    assert parse_action("null") is None
    assert parse_action("42") is None


def test_trailing_text_is_malformed() -> None:
    raw = '{"tool": "reply", "args": {}, "confidence": 0.9, "abstain": false} '
    for tail in ("ok", " }", " // comment", "```"):
        assert parse_action(raw + tail) is None, tail
    # Но whitespace до/после JSON допустим.
    assert parse_action("  \n" + raw.strip() + "\n ") is not None


def test_wrong_types_are_malformed() -> None:
    assert parse_action('{"tool": 1, "args": {}, "confidence": 0.9, "abstain": false}') is None
    assert parse_action('{"tool": "", "args": {}, "confidence": 0.9, "abstain": false}') is None
    assert parse_action('{"tool": null, "args": {}, "confidence": 0.9, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": [], "confidence": 0.9, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": "x", "confidence": 0.9, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": "0.9", "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": 0.9, "abstain": "false"}') is None
    # bool -- подкласс int: confidence=true/abstain=1 должны упасть.
    assert parse_action('{"tool": "reply", "args": {}, "confidence": true, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": 0.9, "abstain": 1}') is None


def test_non_finite_and_out_of_range_confidence_is_malformed() -> None:
    assert parse_action('{"tool": "reply", "args": {}, "confidence": NaN, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": Infinity, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": -Infinity, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": -0.1, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": 1.0000001, "abstain": false}') is None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": 0, "abstain": false}') is not None
    assert parse_action('{"tool": "reply", "args": {}, "confidence": 1, "abstain": false}') is not None


def test_empty_text_and_garbage_are_malformed() -> None:
    assert parse_action("") is None
    assert parse_action("   ") is None
    assert parse_action("хочу к кафе") is None
    assert parse_action("{не json") is None


def test_abstain_beats_confidence_and_tool() -> None:
    """Приоритет причин: abstain=true важнее всего -- даже confidence=1.0."""
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": "cafe"}, '
        '"confidence": 1.0, "abstain": true}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=_TOOLS,
        known_location_ids=_LOCATIONS,
        known_tour_ids=_TOURS,
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_ABSTAIN_FROM_MODEL


def test_low_confidence_beats_tool_checks() -> None:
    """confidence ниже порога важнее легальности инструмента -- но и не
    ниже abstain (test_abstain_beats_confidence_and_tool)."""
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": "ghost"}, '
        '"confidence": 0.1, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=_TOOLS,
        known_location_ids=_LOCATIONS,
        known_tour_ids=_TOURS,
        confidence_threshold=0.5,
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_LOW_CONFIDENCE
    assert "0.10" in verdict.message


def test_illegal_state_is_tool_not_allowed() -> None:
    parsed = parse_action(
        '{"tool": "start_tour", "args": {"tour_id": "lab_demo"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=["reply"],  # start_tour недоступен в этом состоянии
        known_tour_ids=_TOURS,
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_ILLEGAL_STATE


def test_unknown_location_id_is_unknown_id() -> None:
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": "ghost"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=_TOOLS,
        known_location_ids=_LOCATIONS,
        known_tour_ids=_TOURS,
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_UNKNOWN_ID


def test_unknown_tour_id_is_unknown_id() -> None:
    parsed = parse_action(
        '{"tool": "start_tour", "args": {"tour_id": "nope"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=_TOOLS,
        known_tour_ids=_TOURS,
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_UNKNOWN_ID


def test_resolve_pointing_unknown_exhibit_id_is_unknown_id() -> None:
    """Taiga #7: content_id вне каталога экспонатов -> unknown_id, не исполняется."""
    parsed = parse_action(
        '{"tool": "resolve_pointing", "args": {"content_id": "ghost_exhibit"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=[*_TOOLS, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robo_guide"}),
    )
    assert verdict.ok is False
    assert verdict.reason == REASON_UNKNOWN_ID


def test_resolve_pointing_known_exhibit_id_passes() -> None:
    parsed = parse_action(
        '{"tool": "resolve_pointing", "args": {"content_id": "robo_guide"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=[*_TOOLS, "resolve_pointing"],
        known_exhibit_ids=frozenset({"robo_guide"}),
    )
    assert verdict.ok is True


def test_empty_whitelist_skips_membership_check() -> None:
    """Пустой каталог = whitelist не подгружен: членство не проверяется
    (совпадает с семантикой validate_call, не блокирует узлы без семантической карты)."""
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": "whatever"}, '
        '"confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(parsed, tools_allowed=_TOOLS)
    assert verdict.ok is True


def test_missing_location_id_is_invalid_args() -> None:
    parsed = parse_action('{"tool": "guide_to", "args": {}, "confidence": 0.9, "abstain": false}')
    verdict = verify_action(parsed, tools_allowed=_TOOLS)
    assert verdict.ok is False
    assert verdict.reason == REASON_INVALID_ARGS


def test_wrong_arg_type_is_invalid_args() -> None:
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": 7}, "confidence": 0.9, "abstain": false}'
    )
    verdict = verify_action(parsed, tools_allowed=_TOOLS)
    assert verdict.ok is False
    assert verdict.reason == REASON_INVALID_ARGS


def test_unknown_tool_name_is_illegal_state() -> None:
    """tool, которого нет в каталоге вовсе (не в состоянии, а в принципе)."""
    parsed = parse_action('{"tool": "fly", "args": {}, "confidence": 0.9, "abstain": false}')
    verdict = verify_action(parsed, tools_allowed=_TOOLS)
    assert verdict.ok is False
    assert verdict.reason == REASON_ILLEGAL_STATE


def test_confidence_threshold_is_inclusive() -> None:
    """confidence == threshold -- действие ДОПУЩЕНО (порог включительный)."""
    parsed = parse_action(
        '{"tool": "guide_to", "args": {"location_id": "cafe"}, '
        '"confidence": 0.5, "abstain": false}'
    )
    verdict = verify_action(
        parsed,
        tools_allowed=_TOOLS,
        known_location_ids=_LOCATIONS,
        confidence_threshold=0.5,
    )
    assert verdict.ok is True


def _fuzz_random_mutation(rng: random.Random, base: str) -> str:
    """Одна случайная мутация JSON-строки (для property-стиля без hypothesis)."""
    mut = rng.randrange(6)
    if not base:
        return base
    i = rng.randrange(len(base))
    if mut == 0:  # удалить символ
        return base[:i] + base[i + 1 :]
    if mut == 1:  # вставить мусор
        return base[:i] + rng.choice("{}[]:,.truefalx") + base[i:]
    if mut == 2:  # заменить символ
        return base[:i] + rng.choice("{}[]:,.truefalx") + base[i + 1 :]
    if mut == 3:  # хвост
        return base + rng.choice([" ok", " }", "x"])
    if mut == 4:  # префикс
        return rng.choice(["```json", " ", "{"]) + base
    return base[::-1][:i] + base  # перемешивание (маловероятно валидно)


def test_seeded_fuzz_mutations_never_crash_and_stay_binary() -> None:
    """Свойство: на ЛЮБОМ входе `parse_action` либо вернёт ParsedAction
    с согласованными полями, либо None. Крашей/ValueError быть не может.
    (hypothesis недоступен -- deterministic seeded вместо этого.)"""
    seeds = [
        '{"tool": "reply", "args": {}, "confidence": 0.9, "abstain": false}',
        '{"tool": "guide_to", "args": {"location_id": "cafe"}, "confidence": 1, "abstain": true}',
        '{"tool": "start_tour", "args": {"tour_id": "lab_demo"}, "confidence": 0, "abstain": false}',
        '',
        '""',
        '{}',
    ]
    rng = random.Random(20260912)
    checked = 0
    for base in seeds:
        for _ in range(400):
            mutated = _fuzz_random_mutation(rng, base)
            result = parse_action(mutated)
            checked += 1
            if result is not None:
                assert isinstance(result.tool, str) and result.tool
                assert isinstance(result.args, dict)
                assert 0.0 <= result.confidence <= 1.0
                assert isinstance(result.abstain, bool)
    assert checked == len(seeds) * 400


def test_fuzzed_parsed_actions_are_verify_consistent() -> None:
    """Каждый parsed (из fuzz-пула) проходит verify_action без исключений
    и получает либо ok, либо код причины из закрытого набора."""
    from guide_robot_llm.tools.validate import REASONS

    rng = random.Random(99)
    parsed_any = 0
    for _ in range(500):
        tool = rng.choice(["reply", "guide_to", "start_tour", "ghost_tool"])
        if tool == "reply":
            args: object = {}
        elif tool == "guide_to":
            args = {"location_id": rng.choice(["cafe", "exit", "ghost", 7, None])}
        else:
            args = {"tour_id": rng.choice(["lab_demo", "nope", None])}
        confidence = rng.choice([0, 0.1, 0.5, 0.9, 1, 2, -1])
        abstain = rng.choice([True, False])
        text = json.dumps(
            {"tool": tool, "args": args, "confidence": confidence, "abstain": abstain},
            allow_nan=True,
        )
        parsed = parse_action(text)
        if parsed is None:
            continue
        parsed_any += 1
        verdict = verify_action(
            parsed,
            tools_allowed=_TOOLS,
            known_location_ids=_LOCATIONS,
            known_tour_ids=_TOURS,
            confidence_threshold=0.5,
        )
        if not verdict.ok:
            assert verdict.reason in REASONS
            assert verdict.reason != REASON_MALFORMED_OUTPUT
        # Конвейс и вердикт согласованы.
        assert verdict.tool == parsed.tool
        assert verdict.confidence == parsed.confidence
        assert verdict.abstain == parsed.abstain
    assert parsed_any > 0
