"""`dialog_agent_node._tool_describe_scene()` -- регрессии фиксов C1/C2/C3.

C1: кандидаты -- ТОЛЬКО `_visual_candidates(mission)` (текущая остановка
первой, затем экспонаты той же зоны в порядке каталога, без чужих зон и
не-экспонатов). C2: пороги визуального контекста -- из конфига
(`vision.max_candidates` / `vision.max_frame_age_s`), не хардкод 5 / 2.0.
C3: во время хода реюзнятся кадры, уже замороженные `_run_turn`'ом
(ровно один freeze на ход); вне хода -- штатный freeze буфера.

Нода не поднимается: экземпляр собирается через `__new__` с точечными
атрибутами, rclpy нужен только для импорта модуля.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

rclpy = pytest.importorskip("rclpy", reason="dialog_agent_node импортирует rclpy (ROS-only)")
from guide_robot_llm.dialog_agent_node import DialogAgentNode  # noqa: E402
from guide_robot_llm.visual_context import frame_sha256_16  # noqa: E402

_URL_A = "data:image/jpeg;base64,QUJD"
_URL_B = "data:image/jpeg;base64,RUJD"


class _FakeFrameBuffer:
    """freeze() считает вызовы -- C3 проверяет, что их ровно сколько нужно."""

    def __init__(self, frames: list) -> None:
        self._frames = list(frames)
        self.freeze_calls = 0

    def freeze(self, now_s: float) -> list:
        del now_s
        self.freeze_calls += 1
        return list(self._frames)


def _frame(url: str, captured_at: float) -> SimpleNamespace:
    return SimpleNamespace(data_url=url, captured_at=captured_at, payload_bytes=1000)


def _make_node(
    *,
    frame_buffer: _FakeFrameBuffer,
    frozen: tuple | None = None,
    frozen_now: float | None = None,
    max_candidates: int = 5,
    max_frame_age_s: float = 2.0,
) -> DialogAgentNode:
    node = DialogAgentNode.__new__(DialogAgentNode)
    node._locations_catalog = [  # noqa: SLF001
        {"id": "stop_exhibit", "zone": "hall_a", "category": "exhibit"},
        {"id": "neighbour", "zone": "hall_a", "category": "exhibit"},
        {"id": "other_zone", "zone": "hall_b", "category": "exhibit"},
        {"id": "hall_a_door", "zone": "hall_a", "category": "door"},
    ]
    node._location_zone_by_id = {  # noqa: SLF001
        "stop_exhibit": "hall_a",
        "neighbour": "hall_a",
        "other_zone": "hall_b",
        "hall_a_door": "hall_a",
    }
    node._location_name_by_id = {  # noqa: SLF001
        "stop_exhibit": "Экспонат у остановки",
        "neighbour": "Соседний экспонат",
        "other_zone": "Другая зона",
        "hall_a_door": "Дверь",
    }
    node.last_mission_state = lambda: SimpleNamespace(stop_id="stop_exhibit")  # noqa: SLF001
    node._now_s = lambda: 100.0  # noqa: SLF001
    node._vision_max_candidates = max_candidates  # noqa: SLF001
    node._vision_max_frame_age_s = max_frame_age_s  # noqa: SLF001
    node._turn_frozen_frames = frozen  # noqa: SLF001
    node._turn_frozen_now_s = frozen_now  # noqa: SLF001
    node._frame_buffer = frame_buffer  # noqa: SLF001
    return node


# -- C1: кандидаты из каталога через _visual_candidates ----------------------------


def test_candidates_stop_first_then_same_zone_exhibits_only() -> None:
    node = _make_node(frame_buffer=_FakeFrameBuffer([_frame(_URL_A, captured_at=99.9)]))

    result = node._tool_describe_scene({"focus": "что это?"})  # noqa: SLF001

    assert result.ok, result.message
    assert result.data["exhibit_candidates"] == ("stop_exhibit", "neighbour")


# -- C2: конфиг-пороги вместо хардкода ----------------------------------------------


def test_custom_max_candidates_and_stale_age_s_applied() -> None:
    node = _make_node(
        frame_buffer=_FakeFrameBuffer([_frame(_URL_A, captured_at=99.0)]),
        max_candidates=1,
        max_frame_age_s=0.5,
    )

    result = node._tool_describe_scene({})  # noqa: SLF001

    assert result.ok, result.message
    assert result.data["exhibit_candidates"] == ("stop_exhibit",)
    # Кадр возрастом 1.0 с при пороге 0.5 -- stale (дефолтные 2.0 с сочли бы
    # его свежим -- хардкод отловлен этим утверждением).
    assert result.data["quality"] == "stale"


# -- C3: ровно один freeze на ход ----------------------------------------------------


def test_turn_frozen_frames_reused_without_second_freeze() -> None:
    buffer = _FakeFrameBuffer([_frame(_URL_A, captured_at=98.0)])
    node = _make_node(
        frame_buffer=buffer,
        frozen=(_frame(_URL_B, captured_at=99.9),),
        frozen_now=100.0,
    )

    result = node._tool_describe_scene({})  # noqa: SLF001

    assert result.ok, result.message
    assert buffer.freeze_calls == 0
    # В визуальном контексте -- кадр из стэша хода, не из буфера.
    assert frame_sha256_16(_URL_B) in result.data["visual_context"]
    assert frame_sha256_16(_URL_A) not in result.data["visual_context"]


def test_outside_turn_falls_back_to_own_freeze() -> None:
    buffer = _FakeFrameBuffer([_frame(_URL_A, captured_at=99.9)])
    node = _make_node(frame_buffer=buffer)

    result = node._tool_describe_scene({})  # noqa: SLF001

    assert result.ok, result.message
    assert buffer.freeze_calls == 1


def test_no_frames_fails_with_quality_none() -> None:
    node = _make_node(frame_buffer=_FakeFrameBuffer([]))

    result = node._tool_describe_scene({"focus": "что это?"})  # noqa: SLF001

    assert not result.ok
    assert "нет замороженных кадров" in result.message
    assert result.data["quality"] == "none"
    assert result.data["visual_context"] == ""
    assert result.data["exhibit_candidates"] == ()
