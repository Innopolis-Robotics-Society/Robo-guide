"""`dialog.prompt`: системный промпт + инструкции фаз «действие -> реплика»."""

from __future__ import annotations

from guide_robot_llm.dialog.prompt import (
    build_action_instruction,
    build_answer_instruction,
    build_system_prompt,
)
from guide_robot_llm.tools.schema import ToolSpec

_SAY = ToolSpec("say", "Сказать реплику посетителю.", frozenset({0}))
_STOP = ToolSpec("stop_tour", "Прервать текущий тур совсем.", frozenset({1}))
_HIDDEN = ToolSpec("list_locations", "Список локаций.", frozenset({0}), llm_visible=False)


# -- build_system_prompt: только преамбул + каталог локаций/туров --


def test_prompt_starts_with_preamble_verbatim() -> None:
    prompt = build_system_prompt("ПРЕАМБУЛА ТЕКСТ")
    assert prompt.startswith("ПРЕАМБУЛА ТЕКСТ")


def test_prompt_does_not_render_tool_catalog() -> None:
    """CLAUDE_CODE_TASK.md п.2: фаза 1 больше не видит каталог инструментов."""
    prompt = build_system_prompt("x", locations=[], tours=[])

    assert "Доступные инструменты" not in prompt
    assert "stop_tour" not in prompt


def test_locations_catalog_renders_alias_zone_and_category() -> None:
    prompt = build_system_prompt(
        "x",
        locations=[
            {
                "id": "lab_demo",
                "aliases": ["демонстрационная лаборатория"],
                "zone": "hall_1",
                "category": "макеты, роботы",
            },
            {"id": "cafe", "aliases": ["кафе"], "zone": "hall_2", "category": ""},
        ],
    )

    assert "Локации:" in prompt
    assert "- lab_demo (демонстрационная лаборатория; зона hall_1) — макеты, роботы" in prompt
    assert "- cafe (кафе; зона hall_2)" in prompt


def test_exhibit_location_renders_exhibit_id() -> None:
    prompt = build_system_prompt(
        "x",
        locations=[
            {
                "id": "robo_guide",
                "aliases": ["робот-экскурсовод"],
                "zone": "lab",
                "category": "exhibit",
            }
        ],
    )

    assert "- robo_guide (робот-экскурсовод; зона lab) — exhibit, exhibit_id robo_guide" in prompt


def test_locations_catalog_omits_coordinates() -> None:
    prompt = build_system_prompt(
        "x",
        locations=[
            {"id": "lab_demo", "aliases": [], "zone": "", "category": "", "x": 1.0, "y": 2.0}
        ],
    )

    assert "1.0" not in prompt
    assert "2.0" not in prompt


def test_no_locations_omits_locations_section() -> None:
    prompt = build_system_prompt("x")

    assert "Локации:" not in prompt


def test_tours_catalog_renders_id_name_and_stops() -> None:
    prompt = build_system_prompt(
        "x",
        tours=[{"id": "full_tour", "name": "Полная экскурсия", "stops": ["lab_demo", "cafe"]}],
    )

    assert "Туры:" in prompt
    assert "- full_tour «Полная экскурсия»: lab_demo, cafe" in prompt


def test_no_tours_omits_tours_section() -> None:
    prompt = build_system_prompt("x")

    assert "Туры:" not in prompt


def test_non_public_locations_not_passed_do_not_appear() -> None:
    """Фильтр по is_public -- дело вызывающего (`tool_broker`); модуль просто рендерит,
    что дали -- скрытая локация, отсутствующая во входном списке, не появляется."""
    prompt = build_system_prompt(
        "x", locations=[{"id": "lobby", "aliases": [], "zone": "", "category": ""}]
    )

    assert "server_room" not in prompt


def test_prompt_is_deterministic_for_same_arguments() -> None:
    kwargs = {
        "preamble": "x",
        "locations": [{"id": "lab_demo", "aliases": [], "zone": "hall_1", "category": ""}],
        "tours": [{"id": "full_tour", "name": "Тур", "stops": ["lab_demo"]}],
    }
    assert build_system_prompt(**kwargs) == build_system_prompt(**kwargs)


# -- build_action_instruction: каталог инструментов + правила выбора действия --


def test_action_instruction_lists_exactly_given_visible_tools_with_descriptions() -> None:
    instruction = build_action_instruction([_SAY, _STOP])

    assert "- stop_tour: Прервать текущий тур совсем." in instruction


def test_action_instruction_omits_llm_invisible_tools() -> None:
    instruction = build_action_instruction([_SAY, _STOP, _HIDDEN])

    assert "list_locations" not in instruction
    assert "- stop_tour:" in instruction


def test_action_instruction_is_deterministic_for_same_arguments() -> None:
    tool_specs = [_SAY, _STOP]
    assert build_action_instruction(tool_specs) == build_action_instruction(tool_specs)


def test_action_instruction_mentions_json_form_with_tool_and_args() -> None:
    instruction = build_action_instruction([_STOP])
    assert '{"tool"' in instruction
    assert '"think"' not in instruction
    assert '"tool"' in instruction
    assert "reply" in instruction


def test_action_instruction_targets_last_utterance() -> None:
    """Действие выбирается по ПОСЛЕДНЕЙ реплике посетителя -- сказано явно."""
    instruction = build_action_instruction([_STOP])
    assert "ПОСЛЕДНЕЙ реплике" in instruction


def test_action_instruction_tells_model_to_act_on_stated_intent() -> None:
    """Живой баг: модель дважды подряд выбрала noop вместо start_tour, хотя
    посетитель ясно попросил начать экскурсию -- инструкция обязана явно
    запрещать откладывать уже озвученное намерение через noop."""
    instruction = build_action_instruction([_STOP])
    assert "явно попросил действие" in instruction
    assert "выбери именно его" in instruction


def test_action_instruction_directs_motion_during_tour_to_ask_visitor() -> None:
    """stage2 D2: движение во время тура без подтверждения отклоняется брокером --
    инструкция обязана направлять модель на ask_visitor, а не на прямой guide_to."""
    instruction = build_action_instruction([_STOP])
    assert "ask_visitor" in instruction
    assert "guide_to" in instruction


def test_action_instruction_lists_explicit_noop_reasons() -> None:
    """CLAUDE_CODE_TASK_stage1_knowledge.md п.8.2: давление к noop сокращено --
    только "реплики достаточно", без перечисления частных случаев."""
    instruction = build_action_instruction([_STOP])
    for reason in ("приветствие", "светская беседа", "хватает справки", "неразборчива"):
        assert reason in instruction


def test_action_instruction_does_not_discourage_noop_as_a_delay_tactic() -> None:
    """Ослабленное давление против noop (CLAUDE_CODE_TASK.md п.3): убрана
    формулировка «noop -- не способ отложить решение»."""
    instruction = build_action_instruction([_STOP])
    assert "не способ отложить решение" not in instruction


def test_action_instruction_frames_reply_as_default_for_small_talk() -> None:
    """stage3.5 п.1.3: живой баг -- модель тянулась к туровым инструментам на
    обычные реплики (эмоции, комментарии, вопросы) без явной просьбы действовать."""
    instruction = build_action_instruction([_STOP])
    assert "reply" in instruction
    assert "НЕ команды" in instruction


def test_action_instruction_motion_during_tour_names_greeting_and_free_states() -> None:
    """stage3.5 п.4.1: ask_visitor нужен только во время движения/рассказа/начала
    тура -- если робот стоит и свободен, guide_to выполняется сразу."""
    instruction = build_action_instruction([_STOP])
    assert "движения, рассказа или в самом начале тура" in instruction
    assert "стоит и свободен" in instruction


# -- build_answer_instruction: статичная инструкция фазы реплики --


def test_answer_instruction_is_static() -> None:
    assert build_answer_instruction() == build_answer_instruction()


def test_answer_instruction_demands_consistency_and_honesty() -> None:
    instruction = build_answer_instruction()
    assert "согласована" in instruction
    assert "не удалось" in instruction  # честность при провале действия
    assert "переспроси" in instruction  # noop из-за неразборчивой реплики


def test_answer_instruction_forbids_json_in_speech() -> None:
    assert "без JSON" in build_answer_instruction()


def test_answer_instruction_demands_retelling_not_quoting() -> None:
    """CLAUDE_CODE_TASK_stage1_knowledge.md п.8.4: пересказ найденного своими
    словами, не дословное цитирование справки/итога действия."""
    instruction = build_answer_instruction()
    assert "своими словами" in instruction
    assert "не цитируй" in instruction


def test_answer_instruction_forbids_repeating_previous_replies() -> None:
    """stage5 п.2.2: живой баг -- реплики ходов 1/2/4/5/8 почти дословно
    повторяли друг друга, модель отвечала на свои прошлые ответы, не на
    новую реплику посетителя."""
    instruction = build_answer_instruction()
    assert "Не повторяй свои предыдущие реплики" in instruction
    assert "НОВУЮ фразу посетителя" in instruction


def test_answer_instruction_treats_motion_as_a_process_not_a_result() -> None:
    """stage3.5 п.3: живые примеры лжи (ходы 6, 9, 12) -- "мы стоим перед
    экспонатом" в момент старта движения. Робот только начал ехать."""
    instruction = build_answer_instruction()
    assert "guide_to" in instruction
    assert "только НАЧАЛ ехать" in instruction
    assert "мы стоим перед" in instruction
    assert "мы на месте" in instruction
