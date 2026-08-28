"""Валидация вызова инструмента до похода в ROS (llm_plam.md §3/§4).

Чистая логика: принимает уже посчитанные `tools_allowed` и whitelist
локаций/туров как данные, сама за ними в ROS не лезет -- это ответственность
`tool_broker_node.py`. Whitelist локаций собирается через
`~/list_locations(category="")`: пустая category уже фильтрует
`is_public=false` (`guide_robot_semantic_map/lib/locations_io.py:is_visible`),
второй фильтр здесь не нужен.
"""

from __future__ import annotations

__all__ = ["MOTION_TOOLS", "ValidationError", "validate_call"]

# stage2 D1: используется не здесь -- у tool_broker_node.call_tool() для
# гейта "во время тура моторный инструмент только confirmed=True" (см.
# докстринг `validate_call`, регулярка/has_motion_intent отсюда убраны).
MOTION_TOOLS = frozenset({"start_tour", "guide_to", "tour_by_points"})


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
