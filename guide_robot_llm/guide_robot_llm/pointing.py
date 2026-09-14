"""Детерминированная совместная геометрическая резолюция жеста-указания (Taiga #7).

Чистая логика без rclpy и без HTTP: весь ввод приходит уже замороженным на
логической границе хода (тот же момент, что `MissionState`-снимок) — кандидаты
(id/имя/x/y) из каталога семантической карты, поза робота из TF, геометрия
камеры из параметров, жест-указание из наблюдения (`visual_context.Observation`),
реплика (язык), качество кадров (`visual_context.QUALITY_*`). Модуль только
вычисляет ДЕТЕРМИНИРОВАННУЮ резолюцию: один неоднозначный `content_id` ИЛИ
воздержание (нет кандидата / их несколько / кадры непригодны).

Ключевой инвариант (issue #7): id НИКОГДА не выдумывается. Результат — либо
id одного из переданных `candidates`, либо `None`. Совместный скоринг идёт по
всем сигналам сразу (issue #7 «joint-geometry scoring»): язык (имя/алиас
экспоната в реплике), жест (box-указания в кадре), геометрия камеры
(проекция 3D-позиции кандидата в пиксели), поза робота (TF) и позиции
кандидатов (semantic map). Видимость кандидата (наблюдение VLM: видит ли робот
экспонат) — жёсткий гейт: то, что робот не видит (закрыто/не в кадре), не
решается — лучше уточнить, чем угадать.

`data`/`scores` несут ОГРАНИЧЕННУЮ доказательную метадату (баллы 0..1, углы,
IoU), но не сырые кадры посетителя (issue #7 «bounded evidence metadata, not
raw visitor imagery»): ни base64, ни пиксели в резолюцию не попадают.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field

from guide_robot_llm.visual_context import QUALITY_OK

__all__ = [
    "STATUS_RESOLVED",
    "STATUS_AMBIGUOUS",
    "STATUS_NO_CANDIDATE",
    "STATUS_UNUSABLE_FRAMES",
    "RobotPose",
    "CameraGeometry",
    "Candidate",
    "PointingEvidence",
    "PointingContext",
    "PointingBaseContext",
    "ScoredCandidate",
    "PointingResolution",
    "PointingCase",
    "resolve_pointing",
    "compute_pointing_metrics",
    "W_GEOM",
    "W_LANG",
    "NO_BOX_GEOMETRY",
    "DEFAULT_TOPK_MARGIN",
    "DEFAULT_MIN_SCORE",
]

# Статусы резолюции (issue #7). `resolved` -- единственный, при котором
# content_id гарантированно принадлежит списку кандидатов.
STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_NO_CANDIDATE = "no_candidate"
STATUS_UNUSABLE_FRAMES = "unusable_frames"

# Веса совместного скоринга: геометрия (жест + камера + поза + позиции)
# доминирует, язык (имя/алиас в реплике) -- вспомогательный сигнал.
# Видимость -- не вес, а жёсткий гейт (см. `resolve_pointing`).
W_GEOM = 0.7
W_LANG = 0.3
# Жест без box (есть указание, но модель не дала координат): локализацию
# вести нельзя -- нейтральная геометрия для всех в-кадре (0.5): различить
# кандидатов можно только языком/единственностью.
NO_BOX_GEOMETRY = 0.5
# Минимальная разница top1-top2, при которой выбор неоднозначен (issue #7:
# «zero or >1 plausible -> clarification»).
DEFAULT_TOPK_MARGIN = 0.15
# Порог «правдоподобный кандидат»: ниже -- не в счёт (офскрин/далеко от жеста).
DEFAULT_MIN_SCORE = 0.25
# Ширина гауссиана геометрии как доля меньшей стороны кадра (пиксели).
_SIGMA_FRACTION = 0.10
# Кандидат ближе, чем NEAR_CLIP_M от камеры, не проецируется (за стеклом).
_NEAR_CLIP_M = 0.15
# Полуприёмный «размер» кандидата-точки для IoU-диагностики (доля кадра).
_CANDIDATE_HALF_FRACTION = 0.05
# Слова короче этого не считаются матчем имени (против ложных срабатываний).
_MIN_MATCH_WORD = 3


@dataclass(frozen=True)
class RobotPose:
    """Поза робота в map-кадре (TF `map -> base_footprint`)."""

    x: float
    y: float
    yaw: float  # рад, 0 = +x, против часовой


@dataclass(frozen=True)
class CameraGeometry:
    """Модель камеры (калибровка/монтаж из параметров `vision.*`).

    Камера не привязана к TF (см. `guide_robot_bringup/launch/camera.launch.py`
    и README «Визуальная pipeline»): геометрия задаётся явным параметром, не
    вычисляется. `mount_*` -- смещение камеры в base-кадре (x вперёд, y влево,
    z вверх); `yaw_deg` -- поворот относительно носа робота; `pitch_deg` --
    наклон вниз (положительный = смотрит вниз).
    """

    width_px: int
    height_px: int
    hfov_deg: float
    vfov_deg: float
    mount_x: float = 0.0
    mount_y: float = 0.0
    mount_z: float = 1.0
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0


@dataclass(frozen=True)
class Candidate:
    """Кандидат-экспонат: id из семантической карты + позиции + имена."""

    id: str
    name: str
    x: float
    y: float
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PointingEvidence:
    """Жест-указание из наблюдения: есть ли жест + box в кадре (нормализованный).

    `box` -- (x0, y0, x1, y1) в долях кадра [0, 1], x0<x1, y0<y1; `None`, если
    жест есть, но модель не дала координат (тогда геометрия нейтральна).
    """

    present: bool
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class PointingContext:
    """Всё, что резолюция должна знать (заморожено на границе хода)."""

    candidates: tuple[Candidate, ...]
    robot_pose: RobotPose
    camera: CameraGeometry
    pointing: PointingEvidence
    utterance: str
    frame_quality: str  # visual_context.QUALITY_OK/STALE/NONE
    visible_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PointingBaseContext:
    """Статическая часть контекста жеста-указания, известная ДО наблюдения.

    Taiga #7: камера/поза/кандидаты/реплика/качество кадров заморожены на
    границе хода (`dialog_agent_node`) и приходят в `run_turn` как базис.
    Динамическая часть -- сам жест (`PointingEvidence`) и реально видимые id --
    сообщает наблюдение (VLM), которое считается ВНУТРИ `run_turn` ДО фазы
    действия; `run_turn` складывает базис и наблюдение в полный
    `PointingContext` для геометрического гейта. Так наблюдение
    прогоняется один раз (оно же рендерится в промпт), а не дважды.
    """

    candidates: tuple[Candidate, ...]
    robot_pose: RobotPose
    camera: CameraGeometry
    utterance: str
    frame_quality: str  # visual_context.QUALITY_OK/STALE/NONE


@dataclass(frozen=True)
class ScoredCandidate:
    """Ограниченная доказательная метадата по одному кандидату (без кадров)."""

    content_id: str
    score: float
    in_image: bool
    visible: bool
    depth_m: float
    u_px: float
    v_px: float
    geometry: float
    language: float
    iou_with_box: float | None


@dataclass(frozen=True)
class PointingResolution:
    """Итог резолюции: `status` + (для `resolved`) единственный `content_id`."""

    status: str
    content_id: str | None
    scores: tuple[ScoredCandidate, ...] = ()


@dataclass(frozen=True)
class PointingCase:
    """Один golden-replay кейс для метрик: контекст + выбор модели + эталон.

    `expected_content_id=None` -- кейс, где ПРАВИЛЬНО воздержаться
    (неоднозначно/офскрин/устарело/нет в каталоге).
    """

    context: PointingContext
    model_content_id: str
    expected_content_id: str | None


def _angle_diff(a: float, b: float) -> float:
    """Разность углов в [-pi, pi]."""
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def _project(
    candidate: Candidate, pose: RobotPose, camera: CameraGeometry
) -> tuple[bool, float, float, float]:
    """Проецировать позицию кандидата (x, y, 0) в пиксели камеры.

    Возвращает `(in_image, depth_m, u_px, v_px)`. Проекция аналитическая
    (без поворотов-крестов): горизонталь -- через разность азимутов
    (`u = cx + fx*tan(delta_theta)`), вертикаль -- через угол «ниже
    горизонтаминуса» минус наклон камеры. `depth <= NEAR_CLIP` (за спиной/
    вбок) -> `in_image=False`, координаты `nan`.
    """
    yaw = pose.yaw
    cam_x = pose.x + camera.mount_x * math.cos(yaw) - camera.mount_y * math.sin(yaw)
    cam_y = pose.y + camera.mount_x * math.sin(yaw) + camera.mount_y * math.cos(yaw)
    cam_z = camera.mount_z
    heading = yaw + math.radians(camera.yaw_deg)
    pitch = math.radians(camera.pitch_deg)

    dx = candidate.x - cam_x
    dy = candidate.y - cam_y
    d_h = math.hypot(dx, dy)
    if d_h < 1e-9:
        return False, 0.0, float("nan"), float("nan")

    cand_bearing = math.atan2(dy, dx)
    delta_theta = _angle_diff(cand_bearing, heading)
    depth = d_h * math.cos(delta_theta)
    if depth <= _NEAR_CLIP_M:
        return False, depth, float("nan"), float("nan")

    fx = (camera.width_px / 2.0) / math.tan(math.radians(camera.hfov_deg) / 2.0)
    fy = (camera.height_px / 2.0) / math.tan(math.radians(camera.vfov_deg) / 2.0)
    u = camera.width_px / 2.0 + fx * math.tan(delta_theta)
    vert_angle = math.atan2(cam_z, d_h) - pitch
    v = camera.height_px / 2.0 + fy * math.tan(vert_angle)
    in_image = 0.0 <= u <= camera.width_px and 0.0 <= v <= camera.height_px
    return in_image, depth, u, v


def _geometry_score(u: float, v: float, box, camera: CameraGeometry) -> float:
    """Гауссиан по расстоянию проекции до центра box-указания (1.0 = в цели)."""
    if box is None:
        return NO_BOX_GEOMETRY
    x0, y0, x1, y1 = box
    bx = (x0 + x1) / 2.0 * camera.width_px
    by = (y0 + y1) / 2.0 * camera.height_px
    sigma = _SIGMA_FRACTION * min(camera.width_px, camera.height_px)
    dist2 = (u - bx) ** 2 + (v - by) ** 2
    return math.exp(-dist2 / (2.0 * sigma * sigma))


def _iou(u: float, v: float, box, camera: CameraGeometry) -> float | None:
    """IoU между «точкой кандидата» (квадратом) и box-указанием -- диагностика."""
    if box is None:
        return None
    x0, y0, x1, y1 = box
    box_x0, box_y0 = x0 * camera.width_px, y0 * camera.height_px
    box_x1, box_y1 = x1 * camera.width_px, y1 * camera.height_px
    half = _CANDIDATE_HALF_FRACTION * min(camera.width_px, camera.height_px)
    cand_x0, cand_y0 = u - half, v - half
    cand_x1, cand_y1 = u + half, v + half
    inter_w = max(0.0, min(cand_x1, box_x1) - max(cand_x0, box_x0))
    inter_h = max(0.0, min(cand_y1, box_y1) - max(cand_y0, box_y0))
    inter_area = inter_w * inter_h
    cand_area = (2.0 * half) ** 2
    box_area = (box_x1 - box_x0) * (box_y1 - box_y0)
    union = cand_area + box_area - inter_area
    return inter_area / union if union > 0.0 else 0.0


def _norm_words(text: str) -> set[str]:
    """Токены: NFC, lowercase, ё->е, без пунктуации (для совпадения имён)."""
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    words = set()
    for raw in folded.split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) >= _MIN_MATCH_WORD:
            words.add(word)
    return words


def _stem_match(a: str, b: str) -> bool:
    """Грубое словотечное совпадение: общая префиксная часть >= 4 знаков.

    Лексикон русского покрывает падежные формы без морфологизации
    («робот»~«робота», «карта»~«карты»): общее начало от 4 знаков считаем
    тем же словом. Короткие совпадения («р» у «робот»/«расскажи») -- нет.
    """
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n >= 4


def _language_score(candidate: Candidate, utterance: str) -> float:
    """1.0, если в реплике есть слово из имени/алиаса кандидата, иначе 0.0."""
    if not utterance:
        return 0.0
    utterance_words = _norm_words(utterance)
    if not utterance_words:
        return 0.0
    for label in (candidate.name, *candidate.aliases):
        if not label:
            continue
        label_words = _norm_words(label)
        if any(_stem_match(w, n) for w in utterance_words for n in label_words):
            return 1.0
    return 0.0


def resolve_pointing(
    ctx: PointingContext,
    *,
    model_content_id: str,
    topk_margin: float = DEFAULT_TOPK_MARGIN,
    min_score: float = DEFAULT_MIN_SCORE,
) -> PointingResolution:
    """Детерминированная резолюция жеста-указания на `content_id` (issue #7).

    Порядок (политика неоднозначности issue #7): непригодные кадры -> жест
    отсутствует/нет правдоподобных -> 0 кандидатов -> >1 правдоподобных
    (малый разрыв top1-top2) -> конфликт языка/жеста -> модель не выбрала
    единственного лучшего. Только если кандидат ЕДИНСТВЕННО лучший И совпал с
    выбором модели -- `resolved`; во всех остальных случаях -- воздержание
    (идёт в safe fallback: короткое уточнение, никогда не угадывание).

    `model_content_id` -- id, который выбрала модель (VLM) из кандидатов;
    обязан принадлежать списку кандидатов (проверяется валидатором ДОЗДЕСЬ,
    здесь только сверяется с геометрическим лидером).
    """
    if ctx.frame_quality != QUALITY_OK:
        # Устаревшие/отсутствующие кадры -- входное качество, не содержимое
        # (issue #7: «stale frames -> abstain with an input-quality reason»).
        return PointingResolution(status=STATUS_UNUSABLE_FRAMES, content_id=None)
    if not ctx.candidates or not ctx.pointing.present:
        return PointingResolution(status=STATUS_NO_CANDIDATE, content_id=None)

    scored: list[ScoredCandidate] = []
    for cand in ctx.candidates:
        in_image, depth, u, v = _project(cand, ctx.robot_pose, ctx.camera)
        visible = cand.id in ctx.visible_ids
        geometry = _geometry_score(u, v, ctx.pointing.box, ctx.camera) if in_image else 0.0
        language = _language_score(cand, ctx.utterance)
        # Жёсткий гейт: то, что не в кадре ИЛИ что робот не видит (закрыто),
        # не получает балла -- разрешается только видимое и в-кадре.
        score = (W_GEOM * geometry + W_LANG * language) if (in_image and visible) else 0.0
        iou = _iou(u, v, ctx.pointing.box, ctx.camera) if in_image else None
        scored.append(
            ScoredCandidate(
                content_id=cand.id,
                score=round(score, 6),
                in_image=in_image,
                visible=visible,
                depth_m=round(depth, 3),
                u_px=round(u, 2) if in_image else float("nan"),
                v_px=round(v, 2) if in_image else float("nan"),
                geometry=round(geometry, 6),
                language=language,
                iou_with_box=round(iou, 4) if iou is not None else None,
            )
        )

    plausible = [s for s in scored if s.in_image and s.visible and s.score >= min_score]
    if not plausible:
        return PointingResolution(
            status=STATUS_NO_CANDIDATE, content_id=None, scores=tuple(scored)
        )

    ranked = sorted(plausible, key=lambda s: s.score, reverse=True)
    if len(ranked) >= 2 and (ranked[0].score - ranked[1].score) < topk_margin:
        # Два правдоподобных почти на одном уровне -- неоднозначно (issue #7).
        return PointingResolution(status=STATUS_AMBIGUOUS, content_id=None, scores=tuple(scored))

    by_geom = max(plausible, key=lambda s: (s.geometry, s.score))
    by_lang = max(plausible, key=lambda s: (s.language, s.score))
    if by_lang.language > 0.0 and by_geom.content_id != by_lang.content_id:
        # Конфликт языка/жеста: имя в реплике и цель жеста -- разные экспонаты.
        # Лучше уточнить, чем выбрать по одному из сигналов (issue #7).
        return PointingResolution(status=STATUS_AMBIGUOUS, content_id=None, scores=tuple(scored))

    best = ranked[0]
    if best.content_id != model_content_id:
        # Модель выбрала не геометрического лидера (или чужой id) -- не
        # исполняем чужой выбор, уточняем (issue #7: «never fabricate»).
        return PointingResolution(status=STATUS_AMBIGUOUS, content_id=None, scores=tuple(scored))

    return PointingResolution(
        status=STATUS_RESOLVED, content_id=best.content_id, scores=tuple(scored)
    )


def compute_pointing_metrics(cases: list[PointingCase]) -> dict:
    """Метрики issue #7 по golden-replay набору (детерминированно, без LLM).

    - `top1_accuracy` -- доля кейсов С ЭТАЛОННЫМ id, где резолюция `resolved`
      И выбрала правильный id (top-1 content-ID accuracy).
    - `abstention_accuracy` -- доля кейсов, где ПРАВИЛЬНО воздержаться
      (`expected=None`), на которых мы действительно воздержались.
    - `high_confidence_wrong_rate` -- доля РЕШЁННЫХ кейсов, где id не совпал
      с эталоном (ошибка при высокой уверенности).
    - `iou_mean` -- среднее IoU проекции решённого кандидата и box-указания,
      по кейсам, где box был (диагностика «IoU where boxes exist»).
    """
    resolved_total = 0
    top1_correct = 0
    high_conf_wrong = 0
    has_expected = 0
    should_abstain = 0
    abstained_when_should = 0
    iou_values: list[float] = []

    for case in cases:
        resolution = resolve_pointing(case.context, model_content_id=case.model_content_id)
        if case.expected_content_id is not None:
            has_expected += 1
        if case.expected_content_id is None:
            should_abstain += 1
            if resolution.status != STATUS_RESOLVED:
                abstained_when_should += 1
        if resolution.status == STATUS_RESOLVED:
            resolved_total += 1
            if resolution.content_id == case.expected_content_id:
                top1_correct += 1
            else:
                high_conf_wrong += 1
            for s in resolution.scores:
                if s.content_id == resolution.content_id and s.iou_with_box is not None:
                    iou_values.append(s.iou_with_box)

    def _rate(numerator: int, denominator: int) -> float:
        return (numerator / denominator) if denominator else 0.0

    return {
        "top1_accuracy": _rate(top1_correct, has_expected),
        "abstention_accuracy": _rate(abstained_when_should, should_abstain),
        "high_confidence_wrong_rate": _rate(high_conf_wrong, resolved_total),
        "iou_mean": (sum(iou_values) / len(iou_values)) if iou_values else None,
        "resolved_count": resolved_total,
        "total": len(cases),
    }
