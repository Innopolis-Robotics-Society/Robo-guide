"""Визуальный контекст хода: замороженные кадры + кандидаты-экспонаты (Taiga #4).

Чистая логика без rclpy: кадры приходят из `lib/frame_buffer.FrameBuffer.freeze`
(duck-typed `FrozenFrameLike`), кандидаты -- из каталога локаций, который
`dialog_agent_node.py` уже стянул у tool_broker на `on_activate`. Модуль НЕ
знает ROS, не читает время сам и не хранит кадры -- он только раскладывает
уже замороженные данные в детерминированный `VisualTurnContext` и рендерит
волатильный текстовый блок для промпта (кэшируемые стабильные инструкции
собраны отдельно, в `dialog/prompt.py`).

Ключевой инвариант (issue #4): в промпт-путь НЕ попадает ни один id
экспоната, которого нет в списке кандидатов семантической карты. Кандидаты
-- единственный источник id в визуальном контексте; наблюдение модели
(`parse_observation`) фильтрует её «видимые экспонаты» по этому списку и
чужие id выбрасывает (грамматика `build_observation_grammar` уже фиксирует
допустимые строки -- фильтр здесь защита от сервера, проигнорировавшего
грамматику).

`data_url` (base64) в контекст и снимок хода НЕ попадает намеренно:
текстовый лог (`interaction_log`) должен оставаться без base64-контента
(acceptance issue #4) -- в `snap["frames"]` уходит только метаданные
(время, возраст, sha256-отпечаток, размер).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "FrozenFrameLike",
    "FrameMeta",
    "ExhibitCandidate",
    "VisualTurnContext",
    "Observation",
    "ObservationRequest",
    "QUALITY_OK",
    "QUALITY_STALE",
    "QUALITY_NONE",
    "POINTING_NONE",
    "POINTING_YES",
    "POINTING_UNCERTAIN",
    "frame_sha256_16",
    "build_visual_context",
    "parse_observation",
    "render_visual_context",
    "render_observation",
]

# Качество входных кадров для фазы действия/наблюдения:
# ok -- свежие кадры есть; stale -- кадры есть, но не старше порога
# свежести уже не считать нельзя (пометка для модели); none -- кадров нет
# (text-only вариант стратегии).
QUALITY_OK = "ok"
QUALITY_STALE = "stale"
QUALITY_NONE = "none"

# Жест-указание, который модель сообщает в наблюдении:
# none / yes / uncertain (ровно эти 3 строки, GBNF фиксирует).
POINTING_NONE = "none"
POINTING_YES = "yes"
POINTING_UNCERTAIN = "uncertain"
_POINTING_VALUES = frozenset({POINTING_NONE, POINTING_YES, POINTING_UNCERTAIN})

_MAX_PEOPLE_COUNT = 20


class FrozenFrameLike(Protocol):
    """Поля `lib/frame_buffer.FrozenFrame`, которые нужны контексту."""

    data_url: str
    captured_at: float
    payload_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class FrameMeta:
    """Метаданные замороженного кадра БЕЗ base64 (для промпта и лога)."""

    captured_at: float
    age_s: float
    sha256_16: str
    payload_bytes: int
    stale: bool
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class ExhibitCandidate:
    """Кандидат-экспонат: id из семантической карты + человекочитаемое имя."""

    id: str
    name: str
    zone: str = ""


@dataclass(frozen=True)
class VisualTurnContext:
    """Визуальный снимок одного хода: кадры + кандидаты (обрезанные)."""

    frames: tuple[FrameMeta, ...]
    candidates: tuple[ExhibitCandidate, ...]

    @property
    def has_frames(self) -> bool:
        """Есть ли в контексте хотя бы один кадр (иначе -- text-only ход)."""
        return bool(self.frames)

    @property
    def quality(self) -> str:
        """ok/stale/none -- один флаг на ход (для наблюдения и промпта)."""
        if not self.frames:
            return QUALITY_NONE
        return QUALITY_STALE if any(frame.stale for frame in self.frames) else QUALITY_OK


def frame_sha256_16(data_url: str) -> str:
    """Первые 16 hex sha256 base64-payload'а кадра (отпечаток для лога).

    Хешируется payload ПОВЫШЕ ЗАГОЛОВКА (`data:...;base64,`): отпечаток
    стабилен при смене MIME-префикса и не тащит base64 в лог.
    """
    payload = data_url.partition("base64,")[2]
    return hashlib.sha256(payload.encode("ascii", "replace")).hexdigest()[:16]


def build_visual_context(
    frozen: list[FrozenFrameLike],
    *,
    now_s: float,
    candidates: list[ExhibitCandidate],
    max_candidates: int,
    stale_age_s: float,
) -> VisualTurnContext:
    """Собрать контекст хода из замороженных кадров и кандидатов.

    `now_s` -- тот же момент, на котором вызван `FrameBuffer.freeze`
    (одна точка заморозки на ход -- кадры и миссия не разъезжаются).
    Кандидаты обрезаются до `max_candidates` (свежие первые: вызывающий
    код выстраивает их от ближайшего). `stale_age_s` -- порог свежести
    (обычно `vision.max_frame_age_s`): кадр старше порога помечается
    stale и ходит в промпт с пометкой качества, не как «свежий взгляд».
    """
    frames = tuple(
        FrameMeta(
            captured_at=frame.captured_at,
            age_s=max(0.0, round(now_s - frame.captured_at, 1)),
            sha256_16=frame_sha256_16(frame.data_url),
            payload_bytes=int(frame.payload_bytes),
            stale=(now_s - frame.captured_at) >= stale_age_s,
            width=int(getattr(frame, "width", 0)),
            height=int(getattr(frame, "height", 0)),
        )
        for frame in frozen
    )
    return VisualTurnContext(frames=frames, candidates=tuple(candidates[:max_candidates]))


# -- наблюдение (observe_then_decide) ---------------------------------------


@dataclass(frozen=True)
class Observation:
    """Структурированное наблюдение модели над кадрами (после host-фильтра).

    `exhibit_candidates` -- ОТФИЛЬТРОВАННЫЕ по списку кандидатов id:
    модель могла «увидеть» что угодно, в промпт-путь попадает только то,
    что есть в семантической карте (инвариант issue #4).
    """

    people_count: int
    exhibit_candidates: tuple[str, ...]
    pointing_evidence: str
    scene_facts: str


@dataclass(frozen=True)
class ObservationRequest:
    """Всё, что `run_turn` обязан знать, чтобы прогнать фазу наблюдения.

    `instruction` -- СТАБИЛЬНАЯ инструкция (строится один раз на
    `on_activate`, побайтово одинакова между ходами -- CACHE_REUSE).
    `context_text`/`frames`/`grammar`/`candidate_ids` -- волатильные на
    ход: кандидаты зависят от позиции робота, кадры -- от момента
    транскрипта. `quality` -- host-выводное качество входных кадров
    (`VisualTurnContext.quality`), в наблюдение не спрашивается у модели.
    """

    instruction: str
    context_text: str
    frames: tuple[str, ...]
    grammar: str
    candidate_ids: frozenset[str]
    quality: str
    max_chars: int


def parse_observation(
    text: str, *, candidate_ids: frozenset[str], max_chars: int
) -> Observation | None:
    """Строго разобрать наблюдение; `None` -- malformed (фаза деградирует).

    Форма: ровно 4 ключа `people_count`/`exhibit_candidates`/
    `pointing_evidence`/`scene_facts`, без чужих. Правила host-стороны
    (грамма может быть проигнорирована сервером): `people_count` int
    0..20; `pointing_evidence` ровно из {none,yes,uncertain};
    `exhibit_candidates` -- список строк, в котором ОСТАВЛЯЮТСЯ только
    id из `candidate_ids` (порядок модели сохраняется, дубликаты режутся);
    `scene_facts`
    обрезается до `max_chars`. `NaN`/`Infinity` JSON-литералы Python'ом
    парсятся -- отбрасываются проверками типов.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {
        "people_count",
        "exhibit_candidates",
        "pointing_evidence",
        "scene_facts",
    }:
        return None

    people_count = data["people_count"]
    if isinstance(people_count, bool) or not isinstance(people_count, int):
        return None
    if not 0 <= people_count <= _MAX_PEOPLE_COUNT:
        return None

    raw_candidates = data["exhibit_candidates"]
    if not isinstance(raw_candidates, list) or not all(
        isinstance(item, str) for item in raw_candidates
    ):
        return None
    # Дубликаты режутся (инструкция их запрещает, грамма не пиннит -- host
    # остаётся последней линией защиты).
    kept: list[str] = []
    for item in raw_candidates:
        if item in candidate_ids and item not in kept:
            kept.append(item)
    kept_candidates = tuple(kept)

    pointing = data["pointing_evidence"]
    if not isinstance(pointing, str) or pointing not in _POINTING_VALUES:
        return None

    scene_facts = data["scene_facts"]
    if not isinstance(scene_facts, str):
        return None
    return Observation(
        people_count=people_count,
        exhibit_candidates=kept_candidates,
        pointing_evidence=pointing,
        scene_facts=scene_facts[:max_chars],
    )


# -- рендер в промпт ---------------------------------------------------------


def render_visual_context(context: VisualTurnContext, *, utterance: str) -> str:
    """Волатильный текстовый блок визуального контекста для промпта хода.

    Содержит: реплику посетителя (якорь — наблюдение/действие отвечают на
    НУЖНУЮ фразу), метаданные кадров (время/возраст/отпечаток/размер, БЕЗ
    base64) и список кандидатов-экспонатов -- ЕДИНСТВЕННЫЙ источник id
    в визуальном промпте. Пустые кандидаты -- явная инструкция не
    выдумывать id (abstention-friendly форма). Детерминирован: те же
    аргументы -- те же байты.
    """
    lines = ["[Визуальный контекст]"]
    if utterance:
        lines.append(f"Реплика посетителя: «{utterance}»")

    if context.frames:
        frame_parts = ", ".join(
            f"t={frame.captured_at:.1f} возраст {frame.age_s:.1f} с sha16={frame.sha256_16} "
            f"{frame.payload_bytes} Б" + (" (устарел)" if frame.stale else "")
            for frame in context.frames
        )
        lines.append(f"Кадры с камеры: {len(context.frames)} -- {frame_parts}")
        if context.quality == QUALITY_STALE:
            lines.append("Внимание: часть кадров устарела -- опиши только устойчивые детали.")
    else:
        lines.append("Кадры с камеры: нет (text-only) -- визуальных данных не существует.")

    if context.candidates:
        lines.append(
            "Кандидаты-экспонаты (единственный источник id, вне списка -- не существует):"
        )
        for candidate in context.candidates:
            suffix = f", зона {candidate.zone}" if candidate.zone else ""
            lines.append(f"- {candidate.id} «{candidate.name}»{suffix}")
    else:
        lines.append(
            "Кандидаты-экспонаты: нет. id экспонатов/локаций ВЫДУМЫВАТЬ ЗАПРЕЩЕНО -- "
            "действуй только по справке и статусу, при необходимости уточни вопросом."
        )
    return "\n".join(lines)


_QUALITY_RU = {
    QUALITY_OK: "свежие",
    QUALITY_STALE: "устаревшие",
    QUALITY_NONE: "отсутствуют",
}
_POINTING_RU = {
    POINTING_NONE: "не виден",
    POINTING_YES: "есть",
    POINTING_UNCERTAIN: "непонятно",
}


def render_observation(observation: Observation, *, quality: str, max_chars: int) -> str:
    """Детерминированный русский блок наблюдения для суффикса фазы действия.

    `quality` -- host-выводное качество кадров (`VisualTurnContext.quality`),
    не поле модели. `max_chars` -- тот же потолок, что в `parse_observation`
    (двойная страховка на случай, если вызывающий код соберёт блок из
    наблюдения, собранного с другим лимитом).
    """
    lines = ["[Визуальное наблюдение]"]
    lines.append(f"людей в кадре: {observation.people_count}")
    if observation.exhibit_candidates:
        lines.append(
            "видимые экспонаты (id из кандидатов): " + ", ".join(observation.exhibit_candidates)
        )
    else:
        lines.append("видимые экспонаты: не удалось уверенно определить")
    lines.append(
        f"указательный жест: {_POINTING_RU.get(observation.pointing_evidence, 'непонятно')}"
    )
    lines.append(f"качество входных кадров: {_QUALITY_RU.get(quality, quality)}")
    scene = observation.scene_facts[:max_chars].strip()
    if scene:
        lines.append(f"факты сцены: {scene}")
    return "\n".join(lines)
