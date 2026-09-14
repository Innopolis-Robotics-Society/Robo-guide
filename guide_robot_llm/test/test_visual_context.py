"""`visual_context` -- визуальный контекст хода (Taiga #4).

Acceptance issue #4: prompt snapshot-тесты (text-only, 1 кадр, 3 кадра, stale
кадр, пустые кандидаты), candidate-leak (в промпт не попадает id вне списка
кандидатов), стабильность статичных инструкций, strict-парсинг наблюдения с
host-фильтром id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from guide_robot_llm.dialog.prompt import build_observation_instruction
from guide_robot_llm.llm_client.grammar import build_observation_grammar
from guide_robot_llm.visual_context import (
    QUALITY_NONE,
    QUALITY_OK,
    QUALITY_STALE,
    ExhibitCandidate,
    Observation,
    build_visual_context,
    frame_sha256_16,
    parse_observation,
    render_observation,
    render_visual_context,
)

_URL_1 = "data:image/jpeg;base64,QUJD"
_URL_2 = "data:image/jpeg;base64,RUJD"
_URL_3 = "data:image/jpeg;base64,R0JD"
_CANDIDATES = frozenset({"lab105a", "lidar_stand"})


def _frame(data_url: str, captured_at: float, payload_bytes: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(data_url=data_url, captured_at=captured_at, payload_bytes=payload_bytes)


def _context(frames: list, candidates: list) -> object:
    return build_visual_context(
        frames, now_s=100.0, candidates=candidates, max_candidates=5, stale_age_s=2.0
    )


def _candidate(i: int, zone: str = "hall_a") -> ExhibitCandidate:
    return ExhibitCandidate(id=f"loc_{i}", name=f"Имя {i}", zone=zone)


# -- отпечаток кадров ----------------------------------------------------------


def test_frame_sha256_16_deterministic_16_hex_and_ignores_header() -> None:
    digest = frame_sha256_16(_URL_1)

    assert digest == frame_sha256_16("data:image/png;base64,QUJD")
    assert len(digest) == 16
    assert all(char in "0123456789abcdef" for char in digest)
    assert frame_sha256_16("data:image/jpeg;base64,QUJG") != digest


# -- сборка контекста ----------------------------------------------------------


def test_empty_buffer_is_text_only() -> None:
    context = _context([], [])

    assert context.frames == ()
    assert context.candidates == ()
    assert context.has_frames is False
    assert context.quality == QUALITY_NONE


def test_fresh_frame_metadata() -> None:
    context = _context([_frame(_URL_1, captured_at=99.6)], [])

    frame = context.frames[0]
    assert frame.age_s == 0.4
    assert frame.stale is False
    assert frame.payload_bytes == 1000
    assert frame.sha256_16 == frame_sha256_16(_URL_1)
    assert context.quality == QUALITY_OK


def test_stale_at_threshold_and_beyond() -> None:
    at_threshold = _context([_frame(_URL_1, captured_at=98.0)], [])
    beyond = _context([_frame(_URL_1, captured_at=95.0)], [])

    assert at_threshold.frames[0].stale is True
    assert at_threshold.quality == QUALITY_STALE
    assert beyond.frames[0].stale is True
    assert beyond.frames[0].age_s == 5.0


def test_candidates_capped_to_max() -> None:
    context = _context([], [_candidate(i) for i in range(8)])

    assert [c.id for c in context.candidates] == [f"loc_{i}" for i in range(5)]


# -- рендер визуального контекста (prompt snapshot-тесты) ----------------------


def test_render_text_only_snapshot() -> None:
    rendered = render_visual_context(_context([], []), utterance="покажи кандинского")

    assert rendered == (
        "[Визуальный контекст]\n"
        "Реплика посетителя: «покажи кандинского»\n"
        "Кадры с камеры: нет (text-only) -- визуальных данных не существует.\n"
        "Кандидаты-экспонаты: нет. id экспонатов/локаций ВЫДУМЫВАТЬ ЗАПРЕЩЕНО -- "
        "действуй только по справке и статусу, при необходимости уточни вопросом."
    )


def test_render_single_frame_snapshot() -> None:
    context = _context(
        [_frame(_URL_1, captured_at=99.6)],
        [ExhibitCandidate("lab105a", "105А", "hall_a")],
    )
    rendered = render_visual_context(context, utterance="что это?")

    assert (
        f"Кадры с камеры: 1 -- t=99.6 возраст 0.4 с sha16={frame_sha256_16(_URL_1)} 1000 Б"
    ) in rendered
    assert "- lab105a «105А», зона hall_a" in rendered
    assert "устарел" not in rendered
    assert "QUJD" not in rendered  # base64 в текст промпта не попадает


def test_render_three_frames_snapshot() -> None:
    frames = [
        _frame(_URL_1, captured_at=98.3, payload_bytes=100),
        _frame(_URL_2, captured_at=99.1, payload_bytes=200),
        _frame(_URL_3, captured_at=99.9, payload_bytes=300),
    ]
    rendered = render_visual_context(_context(frames, []), utterance="кто это?")

    assert rendered.count("возраст") == 3
    assert "Кадры с камеры: 3 -- " in rendered
    assert f"sha16={frame_sha256_16(_URL_1)} 100 Б" in rendered
    assert f"sha16={frame_sha256_16(_URL_3)} 300 Б" in rendered


def test_render_stale_frame_snapshot() -> None:
    rendered = render_visual_context(
        _context([_frame(_URL_1, captured_at=97.0)], []), utterance="привет"
    )

    assert " (устарел)" in rendered
    assert "Внимание: часть кадров устарела -- опиши только устойчивые детали." in rendered


def test_render_is_deterministic() -> None:
    first = render_visual_context(
        _context([_frame(_URL_1, captured_at=99.6)], [_candidate(0)]), utterance="что это?"
    )
    second = render_visual_context(
        _context([_frame(_URL_1, captured_at=99.6)], [_candidate(0)]), utterance="что это?"
    )

    assert first == second


def test_render_candidate_leak_no_foreign_ids() -> None:
    """В рендер не попадает ни один id вне списка кандидатов (acceptance #4)."""
    candidates = [
        ExhibitCandidate("lab105a", "Лаборатория 105А", "hall_a"),
        ExhibitCandidate("lidar_stand", "Стойка лидара", "hall_a"),
    ]
    rendered = render_visual_context(_context([], candidates), utterance="покажи робота")

    assert "lab105a" in rendered and "lidar_stand" in rendered
    for foreign_id in ("kandinsky_viii", "cafe", "entrance", "home"):
        assert foreign_id not in rendered


# -- парсинг наблюдения --------------------------------------------------------


_VALID_OBSERVATION = (
    '{"people_count": 2, "exhibit_candidates": ["lab105a"], '
    '"pointing_evidence": "yes", "scene_facts": "человек указывает на стойку"}'
)


def test_parse_observation_valid() -> None:
    observation = parse_observation(_VALID_OBSERVATION, candidate_ids=_CANDIDATES, max_chars=400)

    assert observation == Observation(2, ("lab105a",), "yes", "человек указывает на стойку")


def test_parse_observation_filters_foreign_ids_keeps_order() -> None:
    text = (
        '{"people_count": 1, "exhibit_candidates": ["lab105a", "kandinsky_viii", '
        '"lidar_stand"], "pointing_evidence": "none", "scene_facts": ""}'
    )
    observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=400)

    assert observation is not None
    assert observation.exhibit_candidates == ("lab105a", "lidar_stand")


def test_parse_observation_empty_known_ids_kept_empty() -> None:
    text = (
        '{"people_count": 0, "exhibit_candidates": ["lab105a"], '
        '"pointing_evidence": "uncertain", "scene_facts": "кадры не разобрать"}'
    )
    observation = parse_observation(text, candidate_ids=frozenset(), max_chars=400)

    assert observation is not None
    assert observation.exhibit_candidates == ()


def test_parse_observation_dedupes_repeated_candidate_ids() -> None:
    # Грамма дубликаты не пиннит (модель может повторить id) -- host режет.
    text = (
        '{"people_count": 1, "exhibit_candidates": ["lab105a", "lab105a"], '
        '"pointing_evidence": "none", "scene_facts": ""}'
    )
    observation = parse_observation(text, candidate_ids=frozenset({"lab105a"}), max_chars=400)
    assert observation is not None
    assert observation.exhibit_candidates == ("lab105a",)


def test_parse_observation_truncates_scene_facts() -> None:
    text = (
        '{"people_count": 0, "exhibit_candidates": [], '
        '"pointing_evidence": "none", "scene_facts": "абвгдежз"}'
    )
    observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=4)

    assert observation is not None
    assert observation.scene_facts == "абвг"


# -- Taiga #7: pointing_box (нормированный бокс жеста-указания) -----------------


def test_parse_observation_with_valid_pointing_box() -> None:
    text = (
        '{"people_count": 1, "exhibit_candidates": ["lab105a"], '
        '"pointing_evidence": "yes", "pointing_box": [0.45, 0.69, 0.55, 0.79], '
        '"scene_facts": "жест на стойку"}'
    )
    observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=400)

    assert observation is not None
    assert observation.pointing_box == (0.45, 0.69, 0.55, 0.79)


def test_parse_observation_box_only_kept_for_confirmed_gesture() -> None:
    """Бокс осмысленен только при pointing_evidence == \"yes\"; иначе -- None."""
    for evidence in ("none", "uncertain"):
        text = (
            '{"people_count": 1, "exhibit_candidates": ["lab105a"], '
            f'"pointing_evidence": "{evidence}", '
            '"pointing_box": [0.1, 0.2, 0.3, 0.4], "scene_facts": ""}'
        )
        observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=400)
        assert observation is not None
        assert observation.pointing_box is None


def test_parse_observation_null_pointing_box_is_none() -> None:
    text = (
        '{"people_count": 1, "exhibit_candidates": ["lab105a"], '
        '"pointing_evidence": "yes", "pointing_box": null, "scene_facts": ""}'
    )
    observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=400)

    assert observation is not None
    assert observation.pointing_box is None


@pytest.mark.parametrize(
    "bad_box",
    [
        "[0.9, 0.2, 0.3, 0.4]",  # x0 > x1
        "[0.2, 0.9, 0.4, 0.3]",  # y0 > y1
        "[0.0, 0.0, 1.5, 0.4]",  # вне [0, 1]
        "[0.1, 0.2, 0.3]",  # 3 числа
        "[0.1, 0.2, 0.3, 0.4, 0.5]",  # 5 чисел
        "[0.1, \"x\", 0.3, 0.4]",  # не число
        "0.5",  # не массив
    ],
)
def test_parse_observation_malformed_box_degrades_to_none(bad_box: str) -> None:
    """Некорректный бокс НЕ отбрасывает наблюдение -- просто box=None (жест есть)."""
    text = (
        '{"people_count": 1, "exhibit_candidates": ["lab105a"], '
        f'"pointing_evidence": "yes", "pointing_box": {bad_box}, "scene_facts": "жест"}}'
    )
    observation = parse_observation(text, candidate_ids=_CANDIDATES, max_chars=400)

    assert observation is not None
    assert observation.people_count == 1
    assert observation.pointing_box is None


def test_parse_observation_four_field_still_valid() -> None:
    """Обратная совместимость: наблюдение БЕЗ pointing_box (4 поля) валидно."""
    observation = parse_observation(
        _VALID_OBSERVATION, candidate_ids=_CANDIDATES, max_chars=400
    )
    assert observation is not None
    assert observation.pointing_box is None


@pytest.mark.parametrize(
    "mutated",
    [
        '{"people_count": 2}',
        '{"people_count": 2, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": "", "extra": 1}',
        '{"people_count": true, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": 2.5, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": -1, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": 21, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": 2, "exhibit_candidates": "lab105a", "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": 2, "exhibit_candidates": [42], "pointing_evidence": "none", '
        '"scene_facts": ""}',
        '{"people_count": 2, "exhibit_candidates": [], "pointing_evidence": "maybe", '
        '"scene_facts": ""}',
        '{"people_count": 2, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": 7}',
        "[2]",
        '{"people_count": 2, "exhibit_candidates": [], "pointing_evidence": "none", '
        '"scene_facts": ""} хвост',
    ],
)
def test_parse_observation_malformed_returns_none(mutated: str) -> None:
    assert parse_observation(mutated, candidate_ids=_CANDIDATES, max_chars=400) is None


# -- рендер наблюдения ----------------------------------------------------------


def test_render_observation_full_block() -> None:
    observation = Observation(2, ("lab105a", "lidar_stand"), "yes", "человек указывает")

    rendered = render_observation(observation, quality=QUALITY_OK, max_chars=400)

    assert rendered == (
        "[Визуальное наблюдение]\n"
        "людей в кадре: 2\n"
        "видимые экспонаты (id из кандидатов): lab105a, lidar_stand\n"
        "указательный жест: есть\n"
        "качество входных кадров: свежие\n"
        "факты сцены: человек указывает"
    )


def test_render_observation_empty_and_quality_variants() -> None:
    empty = Observation(0, (), "uncertain", "кадры не разобрать")

    fresh = render_observation(empty, quality=QUALITY_OK, max_chars=400)
    assert "людей в кадре: 0" in fresh
    assert "качество входных кадров: свежие" in fresh
    assert "факты сцены: кадры не разобрать" in fresh

    assert "видимые экспонаты: не удалось уверенно определить" in fresh
    assert "указательный жест: непонятно" in fresh

    stale = render_observation(empty, quality=QUALITY_STALE, max_chars=400)
    assert "качество входных кадров: устаревшие" in stale

    truncated = render_observation(
        Observation(0, (), "none", "абвгдежз"), quality=QUALITY_OK, max_chars=4
    )
    assert "факты сцены: абвг" in truncated


def test_render_observation_with_pointing_box() -> None:
    """Taiga #7: бокс жеста рендерится в строке «указательный жест»."""
    observation = Observation(1, ("lab105a",), "yes", "жест на стойку", (0.45, 0.69, 0.55, 0.79))

    rendered = render_observation(observation, quality=QUALITY_OK, max_chars=400)

    assert "указательный жест: есть, бокс [0.45, 0.69, 0.55, 0.79]" in rendered


# -- стабильность инструкций и грамматика (CACHE_REUSE, id-пиннинг) --------------


def test_observation_instruction_stable() -> None:
    assert build_observation_instruction() == build_observation_instruction()
    for key in (
        "people_count",
        "exhibit_candidates",
        "pointing_evidence",
        "pointing_box",
        "scene_facts",
    ):
        assert key in build_observation_instruction()


def test_observation_grammar_pins_only_known_ids() -> None:
    grammar = build_observation_grammar(["lab105a", "lidar_stand"])

    assert "candidate-id" in grammar
    assert "lab105a" in grammar and "lidar_stand" in grammar
    assert "kandinsky" not in grammar
    assert build_observation_grammar([]) == build_observation_grammar([])


def test_observation_grammar_empty_candidates_has_no_dead_rule() -> None:
    # Пустые кандидаты: правило candidate-id не объявляется (мёртвое правило
    # с пустой альтернативой не должно попадать в GBNF).
    assert "candidate-id" not in build_observation_grammar([])


# -- конфиг-пороги describe_scene (C2): произвольные max_candidates/stale_age_s ----


def test_candidates_capped_to_custom_max() -> None:
    context = build_visual_context(
        [],
        now_s=100.0,
        candidates=[_candidate(i) for i in range(3)],
        max_candidates=2,
        stale_age_s=2.0,
    )

    assert [c.id for c in context.candidates] == ["loc_0", "loc_1"]


def test_frame_meta_carries_dimensions() -> None:
    frame = _frame(_URL_1, captured_at=99.0, payload_bytes=1000)
    frame.width, frame.height = 640, 480  # SimpleNamespace: просто присвоить
    ctx = _context([frame], candidates=[])
    fm = ctx.frames[0]
    assert fm.width == 640 and fm.height == 480


def test_custom_stale_age_s_marks_frame_stale() -> None:
    """vision.max_frame_age_s приходит конфигом (describe_scene C2) --
    произвольный порог работает так же, как дефолтные 2.0 с."""
    context = build_visual_context(
        [_frame(_URL_1, captured_at=99.0)],
        now_s=100.0,
        candidates=[],
        max_candidates=5,
        stale_age_s=0.5,
    )

    assert context.frames[0].stale is True
    assert context.quality == QUALITY_STALE


def test_observation_grammar_has_pointing_box_rule() -> None:
    """Taiga #7: грамма наблюдения фиксирует бокс жеста (null или 4 координаты)."""
    grammar = build_observation_grammar(["lab105a"])

    assert "pointing-box" in grammar
    assert "pointing-coord" in grammar
    assert '"pointing_box"' in grammar


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
