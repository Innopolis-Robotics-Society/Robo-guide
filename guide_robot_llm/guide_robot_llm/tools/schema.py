"""Каталог инструментов ЛЛМ + таблица гейтов по MissionState.state (DIALOG_REWORK_PLAN.md §4.3).

Гейт по состоянию живёт здесь, не в промпте и не в FSM: ЛЛМ, попросивший
`start_tour` во время тура, получает не REJECT от FSM, а внятный результат
«тур уже идёт, доступно: ...». Один источник для двух потребителей:
`tool_broker_node.call_tool()` дёргает `is_tool_allowed` перед походом в
ROS (с `llm_only=False` -- гейт по состоянию действует на всех
вызывающих, включая сам `dialog_agent`, зовущий невидимый модели `say`),
`dialog_agent_node.py` берёт `allowed_tools(state, llm_only=True)` для
GBNF-каталога и `tools_allowed` в снимке.

`llm_visible=False` (DIALOG_REWORK_PLAN.md §0): `say` перестал быть
выбором модели -- реплику определяет фаза 1 хода (свободный текст), а
`say` в `tool_broker` только озвучивает уже готовый текст, зовёт его сам
`dialog_agent`, не модель. `list_locations`/`list_tours`/`estimate_route`
скрыты по той же причине, что убрано ReAct: каталог локаций/туров теперь
целиком в системном промпте (`dialog/prompt.py`), а не read-only вызов в
рантайме; `estimate_route` продолжает использоваться внутри
`_tool_tour_by_points`, но не как отдельный вызов модели.

`pause`/`hold_position` разрешены только в NARRATING/NAVIGATING
соответственно не произвольно -- это единственные состояния, которые
реально вычитывают `FsmContext.take_pause_request()`
(guide_robot_mission_control/fsm/states/narrating.py,
fsm/states/navigating.py, stage2 D3); в остальных состояниях запрос
молча повис бы, гейтить нужно тут, а не полагаться на то, что FSM
промолчит. Оба маппятся на один и тот же `~/request_pause` брокером
(`tool_broker_node.py::_HANDLERS`) -- различие для модели только в имени
и гейте. `tell_about` разрешён только вне тура -- вне тура
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
    """Один инструмент каталога: имя + состояния, в которых он разрешён + видимость модели.

    `llm_visible=False` -- инструмент существует и гейтится как обычно, но
    не попадает в каталог, который видит ЛЛМ (`allowed_tools(..., llm_only=True)`):
    `tool_broker.call_tool()` по-прежнему его принимает от `dialog_agent`.

    `read_only=True` -- вызов ничего не меняет в mission/навигации, только
    читает `guide_robot_semantic_map` (CLAUDE_CODE_TASK_stage1_knowledge.md
    п.6.6). `dialog/turn.py` рендерит итог такого вызова фазе реплики
    полным текстом (`chunks`/`hits`), а не строкой `выполнено: name(...)` --
    иначе посетитель не услышал бы найденные факты.
    """

    name: str
    description: str
    allowed_states: frozenset[int]
    llm_visible: bool = True
    read_only: bool = False


TOOLS: tuple[ToolSpec, ...] = (
    # stage3.5 п.1.2: reply первым в каталоге -- дефолтный выбор для беседы,
    # модель должна видеть его раньше моторных инструментов, не после них.
    ToolSpec(
        "reply",
        "Просто ответить собеседнику. Выбор ПО УМОЛЧАНИЮ: приветствия, "
        "светская беседа, эмоции и восклицания («ого», «круто», «оно "
        "умное»), вопросы, на которые хватает справки, шутки. Робот при "
        "этом продолжает делать то, что делал.",
        ALL_STATES,
    ),
    ToolSpec(
        "stop_tour",
        "Прервать текущий тур совсем. Только по однозначной просьбе "
        "закончить. Эмоции и комментарии — это reply.",
        _TOUR_ACTIVE_STATES,
    ),
    ToolSpec(
        "pause", "Приостановить рассказ (посетитель отошёл).", frozenset({_S.STATE_NARRATING})
    ),
    ToolSpec(
        "hold_position",
        "Остановиться на месте во время движения, не отменяя тур "
        "(«постой», «подожди секунду»). Не для аварийной остановки.",
        frozenset({_S.STATE_NAVIGATING}),
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
    ToolSpec(
        "say",
        "Сказать реплику посетителю (не рассказ экспоната).",
        ALL_STATES,
        llm_visible=False,
    ),
    ToolSpec(
        "tell_about",
        "Официальный полный рассказ про экспонат голосом робота (как во "
        "время тура). Только вне тура и только по явной просьбе рассказать "
        "целиком. Для ответа на вопрос — reply, ответь сам по справке.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec(
        "ask_visitor",
        "Задать посетителю уточняющий вопрос перед действием, требующим "
        "подтверждения (например, движением во время тура). question — сам "
        "вопрос; on_yes — {tool, args} инструмента, который выполнится при "
        "ответе «да»; on_no — реплика при ответе «нет».",
        ALL_STATES,
    ),
    ToolSpec(
        "lookup_content",
        "Получить полный выверенный текст про экспонат, площадку или город "
        "по content_id из каталога, когда посетитель просит рассказать "
        "подробнее. Не заменяет tell_about: результат ты пересказываешь сам.",
        ALL_STATES,
        read_only=True,
    ),
    ToolSpec(
        "search_content",
        "Найти факты по свободному вопросу, если в справке к реплике "
        "нужного нет.",
        ALL_STATES,
        read_only=True,
    ),
    ToolSpec(
        "resolve_location",
        "Уточнить, какую локацию имеет в виду посетитель, если название не "
        "совпадает с каталогом.",
        ALL_STATES,
        read_only=True,
    ),
    ToolSpec(
        "list_locations",
        "Список локаций (read-only, только публичные).",
        ALL_STATES,
        llm_visible=False,
        read_only=True,
    ),
    ToolSpec(
        "list_tours",
        "Список заранее заданных туров (read-only).",
        ALL_STATES,
        llm_visible=False,
        read_only=True,
    ),
    ToolSpec(
        "estimate_route",
        "Оценить маршрут по списку локаций (read-only).",
        ALL_STATES,
        llm_visible=False,
        read_only=True,
    ),
    # stage3.5 п.1.2: моторные -- последними в каталоге, после reply и
    # read-only справочников, не первыми: модель не должна видеть их как
    # предпочтительный выбор по умолчанию.
    ToolSpec(
        "start_tour",
        "Начать заранее заданный тур по tour_id. Только если посетитель "
        "явно попросил начать экскурсию или тур, не из приветствия, "
        "«повтори» или светской беседы.",
        frozenset({_S.STATE_IDLE}),
    ),
    ToolSpec(
        "guide_to",
        "Провести посетителя к одной локации (location_id), без полного тура. "
        "Во время тура прерывает тур и везёт к локации — сначала спроси "
        "через ask_visitor, прервать ли экскурсию, в on_yes положи guide_to.",
        ALL_STATES,
    ),
    ToolSpec(
        "tour_by_points",
        "Построить маршрут по списку локаций (location_ids) и начать тур. "
        "Только по явной просьбе составить маршрут или экскурсию.",
        frozenset({_S.STATE_IDLE}),
    ),
)

_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def tool_spec(name: str) -> ToolSpec | None:
    """Декларация инструмента по имени, либо None, если такого нет в каталоге."""
    return _BY_NAME.get(name)


def is_tool_allowed(name: str, mission_state: int) -> bool:
    """Проверить, разрешён ли инструмент `name` при текущем `MissionState.state`."""
    spec = _BY_NAME.get(name)
    return spec is not None and mission_state in spec.allowed_states


def allowed_tools(mission_state: int, *, llm_only: bool = False) -> list[str]:
    """Имена инструментов, разрешённых при текущем `MissionState.state`.

    `llm_only=True` -- дополнительно отфильтровать по `llm_visible` (для
    GBNF-каталога и `tools_allowed` в снимке, которые видит модель).
    `tool_broker.call_tool()` зовёт с `llm_only=False` (по умолчанию): гейт
    по состоянию действует на всех вызывающих, а `say` от `dialog_agent`
    обязан пройти, даже не будучи виден модели.
    """
    return [
        tool.name
        for tool in TOOLS
        if mission_state in tool.allowed_states and (not llm_only or tool.llm_visible)
    ]
