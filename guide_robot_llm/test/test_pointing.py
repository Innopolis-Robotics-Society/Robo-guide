"""`pointing.resolve_pointing()` -- детерминированная резолюция жеста (Taiga #7).

Golden replay (issue #7 acceptance): obvious pointing, two similar exhibits,
occlusion, off-screen gesture, language-gesture conflict, stale frames, no
catalog match. Property test: выданный `content_id` всегда принадлежит списку
кандидатов (id не выдумывается). Ambiguity test: неоднозначные случаи дают
воздержание, не догадку. Метрики: top-1 / abstention accuracy /
high-confidence-wrong / IoU.
"""

from __future__ import annotations

import pytest
from guide_robot_llm.pointing import (
    STATUS_AMBIGUOUS,
    STATUS_NO_CANDIDATE,
    STATUS_RESOLVED,
    STATUS_UNUSABLE_FRAMES,
    CameraGeometry,
    Candidate,
    PointingCase,
    PointingContext,
    PointingEvidence,
    RobotPose,
    compute_pointing_metrics,
    resolve_pointing,
)

# Общая сцена: камера на 1 м, смотрит вперёд робота (yaw 0 = +x) с наклоном 10°.
CAM = CameraGeometry(
    width_px=1280, height_px=720, hfov_deg=60, vfov_deg=34, mount_z=1.0, pitch_deg=10
)
POSE = RobotPose(x=0.0, y=0.0, yaw=0.0)


def _cand(cid: str, name: str, x: float, y: float, aliases: tuple = ()) -> Candidate:
    return Candidate(id=cid, name=name, x=x, y=y, aliases=aliases)


def _ctx(
    cands,
    *,
    box=None,
    present: bool = True,
    quality: str = "ok",
    visible=(),
    utterance: str = "",
) -> PointingContext:
    return PointingContext(
        candidates=tuple(cands),
        robot_pose=POSE,
        camera=CAM,
        pointing=PointingEvidence(present=present, box=box),
        utterance=utterance,
        frame_quality=quality,
        visible_ids=frozenset(visible),
    )


def _case(ctx: PointingContext, model_id: str, expected: str | None) -> PointingCase:
    return PointingCase(context=ctx, model_content_id=model_id, expected_content_id=expected)


# ---------------------------------------------------------------------------
# Golden replay: каждый случай issue #7
# ---------------------------------------------------------------------------


def test_obvious_pointing_resolves_to_pointed_exhibit() -> None:
    """Единственный видимый экспонат, жест точно на нём -- решается."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    ctx = _ctx(
        [robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",), utterance="расскажи про этот"
    )

    resolution = resolve_pointing(ctx, model_content_id="robot")

    assert resolution.status == STATUS_RESOLVED
    assert resolution.content_id == "robot"


def test_two_similar_exhibits_abstains() -> None:
    """Два похожих экспоната рядом, жест между ними -- неоднозначно, уточняем."""
    left = _cand("left", "синий робот", 3.0, -0.2)
    right = _cand("right", "красный робот", 3.0, 0.2)
    ctx = _ctx([left, right], box=(0.45, 0.70, 0.55, 0.80), visible=("left", "right"))

    resolution = resolve_pointing(ctx, model_content_id="left")

    assert resolution.status == STATUS_AMBIGUOUS
    assert resolution.content_id is None


def test_occlusion_abstains() -> None:
    """Указывают на закрытый экспонат (робот его не видит) -- не угадываем."""
    hidden = _cand("hidden", "робот", 3.0, -0.2)
    other = _cand("other", "карта", 6.0, 2.0)
    ctx = _ctx(
        [hidden, other],
        box=(0.42, 0.70, 0.52, 0.80),
        visible=("other",),  # hidden не в видимых (закрыт)
    )

    resolution = resolve_pointing(ctx, model_content_id="hidden")

    assert resolution.status == STATUS_NO_CANDIDATE
    assert resolution.content_id is None


def test_off_screen_gesture_abstains() -> None:
    """Экспонат за спиной робота (вне поля зрения) -- не в кадре, не решается."""
    behind = _cand("behind", "робот", -3.0, 0.0)
    ctx = _ctx([behind], box=(0.45, 0.69, 0.55, 0.79), visible=("behind",))

    resolution = resolve_pointing(ctx, model_content_id="behind")

    assert resolution.status == STATUS_NO_CANDIDATE
    assert resolution.content_id is None


def test_language_gesture_conflict_abstains() -> None:
    """Имя в реплике и цель жеста -- разные экспонаты -- конфликт, уточняем."""
    named = _cand("named", "робот", 3.0, -0.2)
    pointed = _cand("pointed", "карта", 3.0, 0.2)
    ctx = _ctx(
        [named, pointed],
        box=(0.50, 0.70, 0.60, 0.80),  # жест на pointed (u~714)
        visible=("named", "pointed"),
        utterance="расскажи про робота",  # имя в реплике -- named
    )

    resolution = resolve_pointing(ctx, model_content_id="pointed")

    assert resolution.status == STATUS_AMBIGUOUS
    assert resolution.content_id is None


def test_stale_frames_abstain_with_input_quality() -> None:
    """Устаревшие кадры -- воздержание по входному качеству, не по содержимому."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    ctx = _ctx(
        [robot],
        box=(0.45, 0.69, 0.55, 0.79),
        visible=("robot",),
        quality="stale",
    )

    resolution = resolve_pointing(ctx, model_content_id="robot")

    assert resolution.status == STATUS_UNUSABLE_FRAMES
    assert resolution.content_id is None


def test_no_frames_abstains() -> None:
    """Кадров нет вообще (text-only) -- визуальную резолюцию вести нельзя."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    ctx = _ctx([robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",), quality="none")

    resolution = resolve_pointing(ctx, model_content_id="robot")

    assert resolution.status == STATUS_UNUSABLE_FRAMES
    assert resolution.content_id is None


def test_no_catalog_match_model_choice_never_resolves() -> None:
    """Модель выбрала id, которого нет в кандидатах -- геометрия его не подтверждает."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    ctx = _ctx([robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",))

    resolution = resolve_pointing(ctx, model_content_id="ghost_exhibit")

    # Геометрический лидер -- robot, но модель выбрала чужой id: не исполняем.
    assert resolution.status == STATUS_AMBIGUOUS
    assert resolution.content_id is None


def test_no_pointing_no_candidate() -> None:
    """Жеста нет (pointing.present=False) -- резолвить нечего."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    ctx = _ctx([robot], present=False, visible=("robot",))

    resolution = resolve_pointing(ctx, model_content_id="robot")

    assert resolution.status == STATUS_NO_CANDIDATE
    assert resolution.content_id is None


def test_clear_single_among_distractors_resolves() -> None:
    """Один близкий к жесту, остальные вне кадра -- однозначный выбор."""
    target = _cand("target", "робот", 3.0, 0.0)
    far = _cand("far", "карта", 3.0, 2.0)  # вне кадра (u>width)
    ctx = _ctx([target, far], box=(0.45, 0.69, 0.55, 0.79), visible=("target", "far"))

    resolution = resolve_pointing(ctx, model_content_id="target")

    assert resolution.status == STATUS_RESOLVED
    assert resolution.content_id == "target"


# ---------------------------------------------------------------------------
# Property: выданный id всегда из списка кандидатов (id не выдумывается)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cands", "box", "visible", "utterance", "quality"),
    [
        (["a", "b", "c"], (0.45, 0.70, 0.55, 0.80), ("a", "b", "c"), "", "ok"),
        (["a"], (0.45, 0.69, 0.55, 0.79), ("a",), "расскажи про a", "ok"),
        (["a", "b"], None, ("a", "b"), "", "ok"),
        (["a"], (0.45, 0.70, 0.55, 0.80), (), "", "ok"),
    ],
)
def test_resolved_content_id_always_from_candidates(
    cands, box, visible, utterance, quality
) -> None:
    """Для ЛЮБОГО выбора модели: если статус resolved, id принадлежит кандидатам."""
    candidate_objs = [_cand(cid, cid, 3.0, i - 1.0) for i, cid in enumerate(cands)]
    ctx = _ctx(candidate_objs, box=box, visible=visible, utterance=utterance, quality=quality)

    for model_id in [*cands, "not_in_catalog"]:
        resolution = resolve_pointing(ctx, model_content_id=model_id)
        if resolution.status == STATUS_RESOLVED:
            assert resolution.content_id in cands
            assert resolution.content_id == model_id


# ---------------------------------------------------------------------------
# Ambiguity: неоднозначные случаи дают уточнение, не догадку
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    ["left", "right"],
)
def test_ambiguity_prefers_clarification_over_guessing(model_id) -> None:
    """Модель выбирает любого из двух похожих -- host всё равно уточняет."""
    left = _cand("left", "синий робот", 3.0, -0.2)
    right = _cand("right", "красный робот", 3.0, 0.2)
    ctx = _ctx([left, right], box=(0.45, 0.70, 0.55, 0.80), visible=("left", "right"))

    resolution = resolve_pointing(ctx, model_content_id=model_id)

    assert resolution.status != STATUS_RESOLVED
    assert resolution.content_id is None


# ---------------------------------------------------------------------------
# Метрики (issue #7: top-1 / abstention accuracy / high-conf-wrong / IoU)
# ---------------------------------------------------------------------------


def _golden_cases() -> list[PointingCase]:
    """Frozen golden-набор: 2 решаемых + 6, где правильно воздержаться."""
    robot = _cand("robot", "робот", 3.0, 0.0)
    left = _cand("left", "синий робот", 3.0, -0.2)
    right = _cand("right", "красный робот", 3.0, 0.2)
    hidden = _cand("hidden", "робот", 3.0, -0.2)
    other = _cand("other", "карта", 6.0, 2.0)
    behind = _cand("behind", "робот", -3.0, 0.0)
    named = _cand("named", "робот", 3.0, -0.2)
    pointed = _cand("pointed", "карта", 3.0, 0.2)
    target = _cand("target", "робот", 3.0, 0.0)
    far = _cand("far", "карта", 3.0, 2.0)

    return [
        _case(_ctx([robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",)), "robot", "robot"),
        _case(
            _ctx([left, right], box=(0.45, 0.70, 0.55, 0.80), visible=("left", "right")),
            "left",
            None,
        ),
        _case(
            _ctx([hidden, other], box=(0.42, 0.70, 0.52, 0.80), visible=("other",)),
            "hidden",
            None,
        ),
        _case(_ctx([behind], box=(0.45, 0.69, 0.55, 0.79), visible=("behind",)), "behind", None),
        _case(
            _ctx(
                [named, pointed],
                box=(0.50, 0.70, 0.60, 0.80),
                visible=("named", "pointed"),
                utterance="расскажи про робота",
            ),
            "pointed",
            None,
        ),
        _case(
            _ctx([robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",), quality="stale"),
            "robot",
            None,
        ),
        _case(_ctx([robot], box=(0.45, 0.69, 0.55, 0.79), visible=("robot",)), "ghost", None),
        _case(
            _ctx([target, far], box=(0.45, 0.69, 0.55, 0.79), visible=("target", "far")),
            "target",
            "target",
        ),
    ]


def test_metrics_on_golden_set() -> None:
    metrics = compute_pointing_metrics(_golden_cases())

    # Все решаемые кейсы решены верно; все неоднозначные -- воздержание.
    assert metrics["top1_accuracy"] == pytest.approx(1.0)
    assert metrics["abstention_accuracy"] == pytest.approx(1.0)
    assert metrics["high_confidence_wrong_rate"] == pytest.approx(0.0)
    assert metrics["resolved_count"] == 2
    assert metrics["total"] == 8
    # IoU-диагностика существует (были box'ы на решённых кейсах).
    assert metrics["iou_mean"] is not None
    assert 0.0 <= metrics["iou_mean"] <= 1.0


def test_metrics_empty_set_is_safe() -> None:
    metrics = compute_pointing_metrics([])
    assert metrics["total"] == 0
    assert metrics["top1_accuracy"] == 0.0
    assert metrics["iou_mean"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
