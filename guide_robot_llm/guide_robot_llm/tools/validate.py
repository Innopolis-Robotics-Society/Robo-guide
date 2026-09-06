"""Валидация вызова инструмента до похода в ROS (llm_plam.md §3/§4).

Чистая логика: принимает уже посчитанные `tools_allowed` и whitelist
локаций/туров как данные, сама за ними в ROS не лезет -- это ответственность
`tool_broker_node.py`. Whitelist локаций собирается через
`~/list_locations(category="")`: пустая category уже фильтрует
`is_public=false` (`guide_robot_semantic_map/lib/locations_io.py:is_visible`),
второй фильтр здесь не нужен.
"""

from __future__ import annotations

__all__ = ["ValidationError", "validate_call"]


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
    """Бросить `ValidationError`, если вызов нельзя отправлять в ROS."""
    if name not in tools_allowed:
        available = ", ".join(tools_allowed) or "(ничего)"
        raise ValidationError(f"{name} сейчас недоступен, доступно: {available}")
    _validate_args(
        name, args, known_location_ids=known_location_ids, known_tour_ids=known_tour_ids
    )


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


def _require_known(value: object, known: frozenset[str], kind: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{kind}: не задан(а)")
    # known пуст -- whitelist не подгружен вызывающим (например, тест
    # инструмента без semantic_map) -- строгую проверку тогда пропускаем,
    # а не считаем всё недействительным.
    if known and value not in known:
        raise ValidationError(f"{kind} {value!r} не найдена")
