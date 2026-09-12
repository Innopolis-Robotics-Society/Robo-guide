"""Валидация действия (контракт ADR-0001) + валидация вызова инструмента.

Два уровня, оба ЧИСТЫЕ (без ROS):

- `parse_action(raw_text)` -- строгий парсер КОНВЕЙСА действия: ровно
  `{"tool", "args", "confidence", "abstain"}`, ничего лишнего (ADR-0001 §2).
  `None` = `malformed_output`.
- `verify_action(parsed, ...)` -- детерминированный вердикт ДО брокера:
  abstain модели -> confidence-порог -> доступность инструмента в
  состоянии -> семантика аргументов. `confidence` НИКОГДА не используется
  как доверие: авторизация только по каталогу, состоянию миссии
  (`tools_allowed`) и валидации аргументов.
- `validate_call(...)` -- прежняя публичная проверка вызова (брокер и
  тесты): поведение и сообщения не меняются (llm_plam.md §3/§4).

Whitelist локаций/туров приходит уже посчитанным (ответственность
`tool_broker_node.py`): пустой whitelist = членство не проверяется.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

__all__ = [
    "MOTION_TOOLS",
    "ValidationError",
    "validate_call",
    "ParsedAction",
    "ActionVerdict",
    "parse_action",
    "verify_action",
    "REASON_MALFORMED_OUTPUT",
    "REASON_LOW_CONFIDENCE",
    "REASON_UNKNOWN_ID",
    "REASON_ILLEGAL_STATE",
    "REASON_ABSTAIN_FROM_MODEL",
    "REASON_INVALID_ARGS",
    "REASONS",
]

# stage2 D1: используется не здесь -- у tool_broker_node.call_tool() для
# гейта "во время тура моторный инструмент только confirmed=True" (см.
# докстринг `validate_call`, регулярка/has_motion_intent отсюда убраны).
MOTION_TOOLS = frozenset({"start_tour", "guide_to", "tour_by_points"})

# Коды причин финальные (ADR-0001 §6): единственный источник «почему»
# действия вместо свободного текста.
REASON_MALFORMED_OUTPUT = "malformed_output"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_UNKNOWN_ID = "unknown_id"
REASON_ILLEGAL_STATE = "illegal_state"
REASON_ABSTAIN_FROM_MODEL = "abstain_from_model"
REASON_INVALID_ARGS = "invalid_args"

REASONS = (
    REASON_MALFORMED_OUTPUT,
    REASON_LOW_CONFIDENCE,
    REASON_UNKNOWN_ID,
    REASON_ILLEGAL_STATE,
    REASON_ABSTAIN_FROM_MODEL,
    REASON_INVALID_ARGS,
)


class ValidationError(Exception):
    """Аргументы вызова не прошли валидацию -- сообщение уже пригодно для ответа ЛЛМ."""


def validate_call(
    name: str,
    args: dict,
    *,
    tools_allowed: list[str],
    known_location_ids: frozenset[str] = frozenset(),
    known_tour_ids: frozenset[str] = frozenset(),
) -> None:
    """Бросить `ValidationError`, если вызов нельзя отправлять в ROS.

    Гейт «моторный инструмент только по явной просьбе» (regex по подстроке
    в user_text) убран отсюда (stage2 D1) -- живой баг: он резал ЛЮБОЙ
    текст без ключевых слов, включая подтверждённый через `ask_visitor`
    «да». Новая защита -- `tool_broker_node.call_tool()`'s `confirmed`
    (`CallTool.srv`): вне тура моторный инструмент проходит как есть (цена
    ошибки мала), во время тура -- только с `confirmed=True`, который
    выставляет исключительно исполнение `ask_visitor.on_yes` после ответа
    «да» посетителя. Здесь эта проверка не нужна -- `validate_call` не
    знает о `MissionState`/turn-контексте, только о `tools_allowed`.
    """
    if name not in tools_allowed:
        available = ", ".join(tools_allowed) or "(ничего)"
        raise ValidationError(f"{name} сейчас недоступен, доступно: {available}")
    if name == "ask_visitor":
        _validate_ask_visitor(
            args,
            tools_allowed=tools_allowed,
            known_location_ids=known_location_ids,
            known_tour_ids=known_tour_ids,
        )
        return
    _validate_args(
        name, args, known_location_ids=known_location_ids, known_tour_ids=known_tour_ids
    )


def _validate_ask_visitor(
    args: dict,
    *,
    tools_allowed: list[str],
    known_location_ids: frozenset[str],
    known_tour_ids: frozenset[str],
) -> None:
    """`on_yes` гоняется через обычный `validate_call` -- рекурсия глубиной 1.

    `on_yes.tool != "ask_visitor"` проверяется ДО рекурсии (C1).
    """
    if not str(args.get("question", "")).strip():
        raise ValidationError("ask_visitor: question обязателен")
    on_yes = args.get("on_yes")
    if not isinstance(on_yes, dict):
        raise ValidationError("ask_visitor: on_yes должен быть объектом {tool, args}")
    on_yes_tool = on_yes.get("tool")
    if not isinstance(on_yes_tool, str) or not on_yes_tool:
        raise ValidationError("ask_visitor: on_yes.tool обязателен")
    if on_yes_tool == "ask_visitor":
        raise ValidationError("ask_visitor: on_yes.tool не может быть ask_visitor")
    validate_call(
        on_yes_tool,
        on_yes.get("args") or {},
        tools_allowed=tools_allowed,
        known_location_ids=known_location_ids,
        known_tour_ids=known_tour_ids,
    )
    if not isinstance(args.get("on_no", ""), str):
        raise ValidationError("ask_visitor: on_no должен быть строкой")


def _validate_args(
    name: str, args: dict, *, known_location_ids: frozenset[str], known_tour_ids: frozenset[str]
) -> None:
    if name == "start_tour":
        _require_known(args.get("tour_id"), known_tour_ids, "тур")
    elif name == "guide_to":
        _require_known(args.get("location_id"), known_location_ids, "локация")
    elif name == "tour_by_points":
        ids = args.get("location_ids") or []
        if not ids:
            raise ValidationError("tour_by_points: пустой список локаций")
        for location_id in ids:
            _require_known(location_id, known_location_ids, "локация")
    elif name == "tell_about":
        # exhibit_id -- ключ content_server, не location_server; whitelist
        # экспонатов здесь не строим (narration_server сам отдаёт
        # OUTCOME_REJECTED("exhibit_not_found") на неизвестный id).
        if not str(args.get("exhibit_id", "")).strip():
            raise ValidationError("tell_about: exhibit_id обязателен")
    elif name == "finish_answer":
        # SubmitAnswer.Request.OUTCOME_RESUME_BASE/SKIP_STOP/END_TOUR = 0/1/2.
        if args.get("outcome") not in (0, 1, 2):
            raise ValidationError(
                "finish_answer: outcome должен быть 0 (resume) / 1 (skip_stop) / 2 (end_tour)"
            )
    elif name == "confirm":
        if not isinstance(args.get("yes"), bool):
            raise ValidationError("confirm: аргумент yes должен быть bool")
    elif name == "say":
        if not str(args.get("text", "")).strip():
            raise ValidationError("say: пустой текст")
    elif name in ("estimate_route",):
        if not (args.get("ids") or []):
            raise ValidationError("estimate_route: пустой список локаций")
    elif name == "lookup_content":
        if not str(args.get("content_id", "")).strip():
            raise ValidationError("lookup_content: content_id обязателен")
        if args.get("mode", "full") not in ("short", "full"):
            raise ValidationError("lookup_content: mode должен быть short или full")
    elif name in ("search_content", "resolve_location"):
        if not str(args.get("query", "")).strip():
            raise ValidationError(f"{name}: query обязателен")


def _require_known(value: object, known: frozenset[str], kind: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{kind}: не задан(а)")
    # known пуст -- whitelist не подгружен вызывающим (например, тест
    # инструмента без semantic_map) -- строгую проверку тогда пропускаем,
    # а не считаем всё недействительным.
    if known and value not in known:
        raise ValidationError(f"{kind} {value!r} не найдена")


# ---------------------------------------------------------------------------
# Строгий слой контракта действия (ADR-0001)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedAction:
    """Распарсенный конвейс действия: ровно 4 поля, строгие типы."""

    tool: str
    args: dict
    confidence: float
    abstain: bool


@dataclass(frozen=True)
class ActionVerdict:
    """Вердикт валидатора: пускать в брокер или нет, и почему.

    `reason` -- код из `REASONS` (кроме `malformed_output`, который живёт
    на уровне `parse_action`); `message` -- человекочитаемая причина,
    пригодная для repair-инструкции и лога.
    """

    ok: bool
    reason: str | None
    tool: str
    args: dict
    confidence: float
    abstain: bool
    message: str = ""


def _reject_non_finite(constant: str) -> float:
    """`json.loads` по умолчанию принимает NaN/Infinity -- тут они запрещены."""
    raise ValueError(f"non-finite number in action JSON: {constant}")


def parse_action(raw_text: str) -> ParsedAction | None:
    """Строго разобрать конвейс действия (ADR-0001 §2).

    Отклоняет: чужие ключи (включая устаревший `think`), недостающие поля,
    неверные типы, `NaN`/`Infinity`, `confidence` вне [0, 1] и любой текст
    после JSON. Возврат `None` = `malformed_output`.
    """
    try:
        parsed = json.loads(raw_text.strip(), parse_constant=_reject_non_finite)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"tool", "args", "confidence", "abstain"}:
        return None

    tool = parsed["tool"]
    args = parsed["args"]
    confidence = parsed["confidence"]
    abstain = parsed["abstain"]

    if not isinstance(tool, str) or not tool:
        return None
    if not isinstance(args, dict):
        return None
    # bool -- подкласс int; confidence=true должен упасть, не стать 1.0.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    if not isinstance(abstain, bool):
        return None
    return ParsedAction(tool=tool, args=args, confidence=confidence, abstain=abstain)


def verify_action(
    action: ParsedAction,
    *,
    tools_allowed: list[str],
    known_location_ids: frozenset[str] = frozenset(),
    known_tour_ids: frozenset[str] = frozenset(),
    confidence_threshold: float = 0.5,
) -> ActionVerdict:
    """Детерминированный валидатор МЕЖДУ выходом модели и `tool_broker`.

    Порядок проверок (приоритет причин): явный abstain модели -> порог
    confidence -> доступность инструмента в состоянии -> аргументы.
    `confidence` не авторизует: ниже порога действие превращается в safe
    abstention (ADR-0001 §4), выше -- на авторизацию не влияет. Аргументы
    гонятся прежней `validate_call` (та же семантика, что у брокера);
    её `ValidationError` классифицируется: чужой id из каталога --
    `unknown_id`, всё остальное -- `invalid_args`.
    """
    base: dict = {
        "tool": action.tool,
        "args": action.args,
        "confidence": action.confidence,
        "abstain": action.abstain,
    }
    if action.abstain:
        return ActionVerdict(
            ok=False,
            reason=REASON_ABSTAIN_FROM_MODEL,
            message="модель запросила воздержаться (abstain=true)",
            **base,
        )
    if action.confidence < confidence_threshold:
        return ActionVerdict(
            ok=False,
            reason=REASON_LOW_CONFIDENCE,
            message=(
                f"confidence {action.confidence:.2f} ниже порога "
                f"{confidence_threshold:.2f}"
            ),
            **base,
        )
    if action.tool not in tools_allowed:
        return ActionVerdict(
            ok=False,
            reason=REASON_ILLEGAL_STATE,
            message=f"{action.tool} сейчас недоступен, доступно: "
            f"{', '.join(tools_allowed) or '(ничего)'}",
            **base,
        )
    try:
        validate_call(
            action.tool,
            action.args,
            tools_allowed=tools_allowed,
            known_location_ids=known_location_ids,
            known_tour_ids=known_tour_ids,
        )
    except ValidationError as error:
        # `_require_known` -- единственный источник «не найдена»; всё
        # остальное -- форма аргумента.
        reason = REASON_UNKNOWN_ID if "не найдена" in str(error) else REASON_INVALID_ARGS
        return ActionVerdict(ok=False, reason=reason, message=str(error), **base)
    return ActionVerdict(ok=True, reason=None, message="", **base)
