"""`dialog.sanitize.sanitize_answer()` -- чистая логика, без ROS (DIALOG_REWORK_PLAN.md §5)."""

from __future__ import annotations

from guide_robot_llm.dialog.sanitize import sanitize_answer


def test_plain_text_passes_through_unchanged() -> None:
    assert sanitize_answer("Привет, я робот-экскурсовод.") == "Привет, я робот-экскурсовод."


# -- защита от утечки tool-call JSON фазы 2 в речь (живой баг: модель однажды
# сгенерировала JSON вместо реплики, и это ушло в TTS дословно) --


def test_tool_call_json_becomes_empty_answer() -> None:
    raw = '{"tool": "starttour", "args": {"tourid": "lab_demo"}}'
    assert sanitize_answer(raw) == ""


def test_tool_call_json_with_nested_args_becomes_empty() -> None:
    raw = '{"tool": "guide_to", "args": {"location_id": "cafe", "greet": true}}'
    assert sanitize_answer(raw) == ""


def test_tool_call_json_with_surrounding_whitespace_becomes_empty() -> None:
    raw = '  \n {"tool": "reply", "args": {}} \n '
    assert sanitize_answer(raw) == ""


def test_ordinary_text_with_colon_is_not_mistaken_for_json() -> None:
    text = "Он сказал: привет, добро пожаловать."
    assert sanitize_answer(text) == text


def test_ordinary_text_with_brace_like_words_is_untouched() -> None:
    text = "Это лаборатория робототехники, а не склад."
    assert sanitize_answer(text) == text


def test_tool_call_json_glued_to_tail_of_text_is_cut() -> None:
    raw = 'Я не знаю, что вы хотите. {"tool": "reply", "args": {}}'
    assert sanitize_answer(raw) == "Я не знаю, что вы хотите."


def test_tool_call_json_at_start_with_trailing_text_is_cut_entirely() -> None:
    raw = '{"tool": "reply", "args": {}} вот и всё.'
    assert sanitize_answer(raw) == ""


def test_tool_call_json_glued_to_middle_of_text_is_cut() -> None:
    raw = 'Хорошо. {"tool": "reply", "args": {}} это лишнее.'
    assert sanitize_answer(raw) == "Хорошо."


def test_braces_that_are_not_a_tool_call_json_are_untouched() -> None:
    text = "Формула выглядит так: {x + y}, ничего особенного."
    assert sanitize_answer(text) == text


def test_valid_json_object_without_tool_key_is_untouched() -> None:
    text = 'Результат такой: {"score": 5} баллов.'
    assert sanitize_answer(text) == text


def test_strips_heading_marker() -> None:
    assert sanitize_answer("# Заголовок\nТекст ответа.") == "Заголовок Текст ответа."


def test_strips_leading_bullet_dash() -> None:
    assert sanitize_answer("- пункт первый\n- пункт второй") == "пункт первый пункт второй"


def test_strips_inline_emphasis_chars() -> None:
    result = sanitize_answer("Это *важно* и _нужно_, поверь мне.")
    assert result == "Это важно и нужно, поверь мне."


def test_strips_backticks() -> None:
    result = sanitize_answer("Команда `start_tour` уже выполнена.")
    assert result == "Команда start_tour уже выполнена."


def test_strips_self_intro_prefix_otvet() -> None:
    assert sanitize_answer("Ответ: привет!") == "привет!"


def test_strips_self_intro_prefix_robot_case_insensitive() -> None:
    assert sanitize_answer("робот: привет, я тут.") == "привет, я тут."


def test_collapses_newlines_into_single_space() -> None:
    assert sanitize_answer("Первая строка.\nВторая строка.") == "Первая строка. Вторая строка."


def test_collapses_multiple_blank_lines() -> None:
    assert sanitize_answer("Первое.\n\n\nВторое.") == "Первое. Второе."


def test_short_text_not_truncated() -> None:
    text = "Короткий ответ."
    assert sanitize_answer(text, max_chars=400) == text


def test_truncates_on_sentence_boundary() -> None:
    text = "Первое предложение. " + "Второе предложение с большим количеством слов внутри. " * 5
    result = sanitize_answer(text, max_chars=40)

    assert result == "Первое предложение."
    assert len(result) <= 40


def test_truncates_on_word_boundary_when_no_sentence_end_in_window() -> None:
    text = "слово " * 30  # без точек в пределах окна
    result = sanitize_answer(text, max_chars=20)

    assert result == "слово слово слово"
    assert not result.endswith(" ")
    assert len(result) <= 20


def test_hard_cut_when_single_word_exceeds_limit() -> None:
    text = "а" * 50
    result = sanitize_answer(text, max_chars=20)

    assert len(result) == 20


def test_markdown_and_self_intro_and_truncation_combined() -> None:
    text = "Ответ: # Заголовок\n- пункт *важный*. " + "Ещё немного текста здесь. " * 5
    result = sanitize_answer(text, max_chars=60)

    assert "Ответ:" not in result
    assert "#" not in result
    assert "*" not in result
    assert len(result) <= 60
