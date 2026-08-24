"""Системный промпт dialog_agent + инструкции фаз хода «действие -> реплика».

Порядок фаз ИНВЕРТИРОВАН против DIALOG_REWORK_PLAN.md §4.2 (живой баг:
реплика «отвожу вас к кафе» + действие noop в том же ходу): сначала фаза
действия -- `{"think": ..., "tool": ..., "args": ...}` под GBNF, затем
исполнение инструмента, и только потом фаза реплики -- свободный текст,
который видит think, выбранное действие и его РЕАЛЬНЫЙ итог. Согласованность
реплики с действием из «просьбы в промпте» стала структурным свойством хода.

Преамбул НЕ хардкодится здесь -- он живёт в
`guide_robot_llm/config/system_prompt.txt`, читается `dialog_agent_node.py`
через параметр `system_prompt_path` и передаётся сюда текстом: тот же файл
(побайтово) должен греть `llm_server/config/system_prompt.txt` -- если
преамбул зашить в код, эти две копии неизбежно разъедутся молча.

Системный промпт целиком -- статическая часть `messages`, обязана идти
ПЕРВОЙ и не меняться от хода к ходу: `CACHE_REUSE` на сервере переиспользует
префикс только если он побайтово совпадает с прошлым разом. Каталог
локаций/туров и справочник (корпус знаний, CLAUDE_CODE_TASK.md пункт 5)
рендерятся один раз на `on_activate` и дальше считаются неизменными до
`on_deactivate` (DIALOG_REWORK_PLAN.md §1) -- координаты в промпт намеренно
не идут.

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
    'ТОЛЬКО одним JSON-объектом вида {"think": "...", "tool": "<имя>", "args": {...}}, '
    'без какого-либо текста до или после него. В поле "think" коротко, одним-двумя '
    "предложениями по-русски, объясни: чего хочет посетитель и почему выбран именно "
    "этот инструмент."
)

_ACTION_NOOP_REASONS = (
    'Выбирай "noop", если: посетитель поздоровался; поблагодарил; сказал светскую '
    "реплику; попросил повторить без явной просьбы начать экскурсию; спросил общий "
    "список экспонатов -- тогда назови их словами из каталога; речь была "
    "неразборчива (переспросишь в ответной реплике); посетитель только что получил "
    "ответ и сам ничего не попросил."
)

# Иначе Gemma отвечает из справочника и берёт noop -- официальный рассказ
# (Narrate) так никогда не стартует.
_ACTION_TELL_ABOUT = (
    "Вопрос или просьба про конкретный экспонат, «кто ты», «расскажи о себе» -- "
    "это tell_about, не noop. exhibit_id совпадает с id локации category=exhibit "
    "из каталога; для «кто ты»/«о себе» бери id робота-экскурсовода (обычно "
    "robo_guide). Не рассказывай экспонат из головы или справочника."
)

# Давление в сторону действия ослаблено (CLAUDE_CODE_TASK.md пункт 3): раньше
# формулировка «noop -- не способ отложить решение» приводила к тому, что
# модель выбирала действие даже при непонятной реплике.
_ACTION_ACT_ON_INTENT = "Если посетитель явно попросил действие -- выбери именно его."

_ANSWER_INSTRUCTION = (
    "Теперь сформулируй короткую реплику посетителю: двумя-тремя короткими "
    "предложениями, только по-русски, без JSON и без упоминания инструментов. "
    "Реплика обязана быть согласована с выбранным действием и его итогом: если "
    "действие выполнено -- скажи, что происходит; если не удалось -- честно скажи "
    "об этом; если действие noop из-за неразборчивой реплики -- коротко переспроси."
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
            _ACTION_TELL_ABOUT,
            _ACTION_ACT_ON_INTENT,
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
    knowledge: str = "",
) -> str:
    """Собрать системный промпт фазы 1: преамбул + каталог локаций/туров + справочник.

    `locations`/`tours` -- элементы в форме, которую отдаёт
    `tool_broker._tool_list_locations`/`_tool_list_tours`
    (`{"id","aliases","zone","category",...}` / `{"id","name","stops"}`).
    `knowledge` -- полный текст корпуса знаний (склейка всех пассажей,
    CLAUDE_CODE_TASK.md пункт 5), статичен -- идёт в кэшируемый префикс.
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
    if knowledge:
        sections.append("Справочник:\n" + knowledge)

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
