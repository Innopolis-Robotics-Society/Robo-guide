"""`llm_client.grammar.build_action_grammar()` -- форма, не содержимое.

Контракт ADR-0001 §2: ровно {tool, args, confidence, abstain}, порядок
полей фиксирован, confidence прижато к [0, 1] на уровне грамматики.
"""

from __future__ import annotations

from guide_robot_llm.llm_client.grammar import build_action_grammar


def test_grammar_contains_root_and_tool_name_rules() -> None:
    grammar = build_action_grammar(["say", "confirm"])

    assert "root ::=" in grammar
    assert "tool-name ::=" in grammar


def test_grammar_lists_exactly_given_tool_names_as_alternatives() -> None:
    grammar = build_action_grammar(["say", "confirm", "stop_tour"])
    tool_name_rule = next(
        line for line in grammar.splitlines() if line.startswith("tool-name ::=")
    )

    assert '"\\"say\\""' in tool_name_rule
    assert '"\\"confirm\\""' in tool_name_rule
    assert '"\\"stop_tour\\""' in tool_name_rule
    # Ничего лишнего -- ровно 3 альтернативы через " | ".
    assert tool_name_rule.count("|") == 2


def test_grammar_args_uses_generic_json_object_not_per_tool_fields() -> None:
    grammar = build_action_grammar(["finish_answer", "tour_by_points"])

    # root ссылается на общее правило object для args -- не на finish_answer-
    # специфичное или tour_by_points-специфичное правило.
    root_rule = next(line for line in grammar.splitlines() if line.startswith("root ::="))
    assert "args" in root_rule
    assert "object" in root_rule
    assert "finish_answer" not in root_rule
    # Ни одно специфичное для конкретного инструмента имя поля не просочилось
    # в грамматику -- args остаётся типизирован только как object везде.
    assert "outcome" not in grammar
    assert "location_ids" not in grammar


def test_grammar_is_stable_regardless_of_tool_order_content() -> None:
    # Форма грамматики (JSON-правила) не зависит от того, какие именно
    # инструменты переданы -- меняется только tool-name.
    grammar_a = build_action_grammar(["say"])
    grammar_b = build_action_grammar(["stop_tour", "pause", "resume"])

    def _without_tool_name_line(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.startswith("tool-name ::=")
        )

    assert _without_tool_name_line(grammar_a) == _without_tool_name_line(grammar_b)


def test_empty_tool_list_still_produces_syntactically_plausible_grammar() -> None:
    grammar = build_action_grammar([])

    assert "root ::=" in grammar
    assert "tool-name ::=" in grammar


def test_grammar_root_has_all_four_contract_fields_in_order() -> None:
    """ADR-0001 §2: tool -> args -> confidence -> abstain, без think."""
    grammar = build_action_grammar(["say"])
    root_rule = next(line for line in grammar.splitlines() if line.startswith("root ::="))

    assert '\\"think\\"' not in root_rule
    assert "think" not in grammar
    assert root_rule.index("tool") < root_rule.index("args")
    assert root_rule.index("args") < root_rule.index("confidence")
    assert root_rule.index("confidence") < root_rule.index("abstain")


def test_grammar_confidence_is_pinned_to_zero_one() -> None:
    """confidence ::= "1" | "0"."<12 знаков>" -- вне [0,1] не сгенерировать."""
    grammar = build_action_grammar(["say"])
    confidence_rule = next(
        line for line in grammar.splitlines() if line.startswith("confidence ::=")
    )

    assert '"1"' in confidence_rule
    assert '"0"' in confidence_rule
    # Никаких [0-9]+ в головах целых/дробных -- только "0" или "1".
    assert "[0-9]+" not in confidence_rule


def test_grammar_abstain_is_boolean_literal() -> None:
    grammar = build_action_grammar(["say"])
    root_rule = next(line for line in grammar.splitlines() if line.startswith("root ::="))

    assert '"true" | "false"' in root_rule
