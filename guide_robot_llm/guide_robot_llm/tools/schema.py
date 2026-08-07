"""Каталог инструментов ЛЛМ + таблица гейтов по MissionState.state (llm_plam.md §4).

Гейт по состоянию живёт здесь, не в промпте и не в FSM (плана §4: "ЛЛМ,
попросивший start_tour во время тура, получает не REJECT от FSM, а внятный
результат «тур уже идёт, доступно: ...» -- и переспланирует"). Один источник
для двух потребителей: `tool_broker_node.call_tool()` дёргает `is_tool_allowed`
перед походом в ROS, `snapshot.py`/будущий промпт (шаг 5) берут `allowed_tools`
для `tools_allowed`.

`pause` разрешён только в NARRATING не произвольно -- это единственное
состояние, которое реально вычитывает `FsmContext.take_pause_request()`
(guide_robot_mission_control/fsm/states/narrating.py); в остальных
состояниях запрос молча повис бы, гейтить нужно тут, а не полагаться на
то, что FSM промолчит. `tell_about` разрешён только вне тура -- вне тура
narration_server свободен (единственный активный Narrate-исполнитель, design
guide_robot_mission_control §4), во время тура он занят остановкой самого
тура и ответит REJECTED("busy").
"""

from __future__ import annotations

from dataclasses import dataclass

from guide_robot_msgs.msg import MissionState

__all__ = ["TOOLS", "ToolSpec", "allowed_tools", "is_tool_allowed", "tool_spec"]

_S = MissionState
ALL_STATES: frozenset[int] = frozenset(
    {
        _S.STATE_IDLE,
        _S.STATE_GREETING,
        _S.STATE_NAVIGATING,
        _S.STATE_NARRATING,
        _S.STATE_ANSWERING,
        _S.STATE_AWAITING_CONFIRM,
        _S.STATE_PAUSED,
        _S.STATE_HELD,
        _S.STATE_RETURNING,
    }
)
_TOUR_ACTIVE_STATES: frozenset[int] = ALL_STATES - {_S.STATE_IDLE}


@dataclass(frozen=True)
class ToolSpec:
    """Один инструмент каталога: имя для ЛЛМ + состояния, в которых он разрешён."""

    name: str
    description: str
    allowed_states: frozenset[int]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "start_tour",
        "Начать заранее заданный тур по tour_id.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec(
        "guide_to",
        "Провести посетителя к одной локации (location_id), без полного тура.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec(
        "tour_by_points",
        "Построить маршрут по списку локаций (location_ids) и начать тур.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec("stop_tour", "Прервать текущий тур совсем.", _TOUR_ACTIVE_STATES),
    ToolSpec(
        "pause", "Приостановить рассказ (посетитель отошёл).", frozenset({_S.STATE_NARRATING})
    ),
    ToolSpec("resume", "Возобновить приостановленный тур.", frozenset({_S.STATE_PAUSED})),
    ToolSpec(
        "confirm",
        "Ответить да/нет на вопрос «Идём дальше?».",
        frozenset({_S.STATE_AWAITING_CONFIRM}),
    ),
    ToolSpec(
        "finish_answer",
        "Закрыть текущий вопрос посетителя: вернуться/пропустить остановку/закончить тур.",
        frozenset({_S.STATE_ANSWERING}),
    ),
    ToolSpec("say", "Сказать реплику посетителю (не рассказ экспоната).", ALL_STATES),
    ToolSpec(
        "tell_about",
        "Рассказать про экспонат (exhibit_id) вне тура.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec("list_locations", "Список локаций (read-only, только публичные).", ALL_STATES),
    ToolSpec("list_tours", "Список заранее заданных туров (read-only).", ALL_STATES),
    ToolSpec("estimate_route", "Оценить маршрут по списку локаций (read-only).", ALL_STATES),
)

_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def tool_spec(name: str) -> ToolSpec | None:
    """Декларация инструмента по имени, либо None, если такого нет в каталоге."""
    return _BY_NAME.get(name)


def is_tool_allowed(name: str, mission_state: int) -> bool:
    """Проверить, разрешён ли инструмент `name` при текущем `MissionState.state`."""
    spec = _BY_NAME.get(name)
    return spec is not None and mission_state in spec.allowed_states


def allowed_tools(mission_state: int) -> list[str]:
    """Имена всех инструментов, разрешённых при текущем `MissionState.state`."""
    return [tool.name for tool in TOOLS if mission_state in tool.allowed_states]
