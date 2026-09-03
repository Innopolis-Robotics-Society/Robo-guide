"""Системный промпт dialog_agent + инструкции фаз хода «действие -> реплика».

Порядок фаз ИНВЕРТИРОВАН против DIALOG_REWORK_PLAN.md §4.2 (живой баг:
реплика «отвожу вас к кафе» + действие noop в том же ходу): сначала фаза
действия -- `{"tool": ..., "args": ...}` под GBNF, затем
исполнение инструмента, и только потом фаза реплики -- свободный текст,
который видит выбранное действие и его РЕАЛЬНЫЙ итог. Согласованность
реплики с действием из «просьбы в промпте» стала структурным свойством хода.

Преамбул НЕ хардкодится здесь -- он живёт в
`guide_robot_llm/config/system_prompt.txt`, читается `dialog_agent_node.py`
через параметр `system_prompt_path` и передаётся сюда текстом: тот же файл
(побайтово) должен греть `llm_server/config/system_prompt.txt` -- если
преамбул зашить в код, эти две копии неизбежно разъедутся молча.

Системный промпт целиком -- статическая часть `messages`, обязана идти
ПЕРВОЙ и не меняться от хода к ходу: `CACHE_REUSE` на сервере переиспользует
префикс только если он побайтово совпадает с прошлым разом. Каталог
локаций/туров рендерится один раз на `on_activate` и дальше считается
неизменным до `on_deactivate` (DIALOG_REWORK_PLAN.md §1) -- координаты в
промпт намеренно не идут. Локальный корпус знаний убран
(CLAUDE_CODE_TASK_stage1_knowledge.md п.5): единственный источник фактов
про экспонаты/площадку/город -- `guide_robot_semantic_map/content/`, за
которым ходят read-only инструменты, а не статичная секция здесь.

`build_action_instruction(tool_specs)`/`build_answer_instruction()` --
вызываются один раз на `on_activate`, результат хранится полями ноды: они
обязаны быть побайтово одинаковыми на каждом ходу, иначе теряется
`CACHE_REUSE` префикса (DIALOG_REWORK_PLAN.md §1, правило 2). Каталог
инструментов рендерится из ВСЕГО `tools.schema.TOOLS`, отфильтрованного
только по `ToolSpec.llm_visible` (не по текущему `tools_allowed` состояния --
та фильтрация уже есть в GBNF-грамматике `build_tool_call_grammar(tool_names)`,
дублировать её текстом незачем и вредно для стабильности байтов инструкции).
"""

from __future__ import annotations

from collections.abc import Sequence

from guide_robot_llm.tools.schema import ToolSpec

__all__ = ["build_action_instruction", "build_answer_instruction", "build_system_prompt"]

_ACTION_HEADER = (
    "Выбери ровно одно действие робота по ПОСЛЕДНЕЙ реплике посетителя -- ответь "
    'ТОЛЬКО одним JSON-объектом вида {"tool": "<имя>", "args": {...}}, '
    "без какого-либо текста до или после него."
)

_ACTION_NOOP_REASONS = (
    'Выбирай "reply", если ответной реплики достаточно: приветствие, светская '
    "беседа, вопрос, на который хватает справки, неразборчивая речь "
    "(переспросишь). «привет» / «здравствуйте» / «как дела» -- ВСЕГДА reply, "
    "никогда guide_to, start_tour или tour_by_points."
)

# Давление в сторону действия ослаблено (CLAUDE_CODE_TASK.md пункт 3): раньше
# формулировка «noop -- не способ отложить решение» приводила к тому, что
# модель выбирала действие даже при непонятной реплике.
_ACTION_ACT_ON_INTENT = (
    "Если посетитель явно попросил действие -- выбери именно его. "
    "«Проведи знакомство / экскурсию / тур …» / «начни экскурсию» -- "
    "start_tour с tour_id из каталога "
    "(например expo_one «Знакомство с Иннополисом»), не tell_about. "
    "tell_about -- только с exhibit_id из каталога локаций (строка-id, не title). "
    "«Вернись домой» / «закончи экскурсию» во время тура -- finish_answer "
    "с outcome=2 или stop_tour, никогда outcome=0."
)

# stage3.5 п.1.3: живой баг -- модель тянулась к туровым инструментам на
# обычные реплики без просьбы что-то сделать (эмоции, комментарии, вопросы).
_ACTION_REPLY_IS_DEFAULT = (
    "Реплика без явной просьбы что-то СДЕЛАТЬ -- это беседа: выбирай reply. "
    "Восхищение, комментарий, вопрос про экспонат -- НЕ команды."
)

# stage2 D2 / stage3.5 п.4.1: guide_to во время движения, рассказа или в
# самом начале тура (GREETING) без подтверждения отклоняется брокером
# (tools/schema.py, tool_broker_node.py) -- ask_visitor единственный путь к
# движению оттуда, инструкция должна явно направлять модель туда. В
# остальных состояниях тура (ANSWERING/AWAITING_CONFIRM/PAUSED/HELD/
# RETURNING) робот и так стоит и ничем не занят -- guide_to выполняется
# сразу, без лишнего вопроса.
_ACTION_MOTION_DURING_TOUR = (
    "ask_visitor для движения нужен ТОЛЬКО когда просьба приходит во время "
    "движения, рассказа или в самом начале тура -- спроси, прервать ли "
    "экскурсию, в on_yes положи guide_to. Если робот стоит и свободен "
    "(например, ждёт ответа на свой вопрос) -- выполняй guide_to сразу, без "
    "вопросов."
)

_ANSWER_INSTRUCTION = (
    "Теперь сформулируй короткую реплику посетителю: двумя-тремя короткими "
    "предложениями, только по-русски, без JSON и без упоминания инструментов. "
    "Реплика обязана быть согласована с выбранным действием и его итогом: если "
    "действие выполнено -- скажи, что происходит; если не удалось -- честно скажи "
    "об этом; если действие reply из-за неразборчивой реплики -- коротко переспроси. "
    "Если в итоге действия или справке есть текст -- перескажи главное своими "
    "словами, 2-3 предложения, не цитируй дословно. Если действие -- движение "
    "(guide_to, start_tour, tour_by_points): робот только НАЧАЛ ехать. Говори "
    "«едем к…», «направляемся…» -- никогда «мы стоим перед», «мы на месте»: на "
    "месте вы окажетесь позже, об этом скажет рассказ у экспоната. Не повторяй "
    "свои предыдущие реплики: каждый ответ -- на НОВУЮ фразу посетителя, "
    "старые ответы уже прозвучали."
)


def build_action_instruction(tool_specs: Sequence[ToolSpec]) -> str:
    """Собрать инструкцию фазы действия: каталог видимых модели инструментов + правила."""
    visible_tools = [spec for spec in tool_specs if spec.llm_visible]
    catalog = "\n".join(f"- {spec.name}: {spec.description}" for spec in visible_tools)
    return "\n\n".join(
        [
            _ACTION_HEADER,
            "Доступные инструменты:\n" + catalog,
            _ACTION_NOOP_REASONS,
            _ACTION_REPLY_IS_DEFAULT,
            _ACTION_ACT_ON_INTENT,
            _ACTION_MOTION_DURING_TOUR,
        ]
    )


def build_answer_instruction() -> str:
    """Собрать инструкцию фазы реплики -- статичный текст.

    Волатильный итог действия вызывающий код (`dialog/turn.py`) приклеивает
    ПОСЛЕ него (правило кэша: статика раньше волатильного).
    """
    return _ANSWER_INSTRUCTION


def build_system_prompt(
    preamble: str,
    *,
    locations: Sequence[dict] = (),
    tours: Sequence[dict] = (),
) -> str:
    """Собрать системный промпт фазы 1: преамбул + каталог локаций/туров.

    `locations`/`tours` -- элементы в форме, которую отдаёт
    `tool_broker._tool_list_locations`/`_tool_list_tours`
    (`{"id","aliases","zone","category",...}` / `{"id","name","stops"}`).
    Факты про экспонаты/площадку/город сюда больше не идут -- источник
    единственный, `guide_robot_semantic_map/content/`, и он читается за
    ход через `lookup_content`/`search_content`, не встраивается в
    статичный промпт целиком (CLAUDE_CODE_TASK_stage1_knowledge.md п.5).
    Пустые аргументы -- соответствующая секция просто не появляется в
    промпте (детерминированный результат для тех же аргументов).
    """
    sections = [preamble]

    if locations:
        rendered_locations = "\n".join(_render_location(loc) for loc in locations)
        sections.append("Локации:\n" + rendered_locations)
    if tours:
        rendered_tours = "\n".join(_render_tour(tour) for tour in tours)
        sections.append("Туры:\n" + rendered_tours)

    return "\n\n".join(sections)


def _render_location(location: dict) -> str:
    aliases = location.get("aliases") or []
    name = aliases[0] if aliases else ""
    zone = location.get("zone") or ""
    category = location.get("category") or ""

    parens_parts = [part for part in (name, f"зона {zone}" if zone else "") if part]
    parens = f" ({'; '.join(parens_parts)})" if parens_parts else ""
    suffix = f" — {category}" if category else ""
    if category == "exhibit":
        suffix += f", exhibit_id {location['id']}"
    return f"- {location['id']}{parens}{suffix}"


def _render_tour(tour: dict) -> str:
    stops = ", ".join(tour.get("stops", []))
    return f"- {tour['id']} «{tour['name']}»: {stops}"
