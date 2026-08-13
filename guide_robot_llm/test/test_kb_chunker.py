"""`kb.chunker.chunk_markdown()` -- чистая логика, без ROS (DIALOG_REWORK_PLAN.md §3.2)."""

from __future__ import annotations

from guide_robot_llm.kb.chunker import chunk_markdown


def test_splits_by_h2_and_h3_headings_with_chain() -> None:
    markdown = (
        "# Иннополис\n\n"
        "## История\n\n"
        "### Основание\n\n"
        "Город основан в две тысячи двенадцатом году указом президента. "
        "Название происходит от слов Иннополис. Это достаточно длинный текст.\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=10)

    assert len(passages) == 1
    assert passages[0].heading == "Иннополис > История > Основание"
    assert "основан" in passages[0].text


def test_h2_without_h3_child_uses_two_level_chain() -> None:
    markdown = "# Иннополис\n\n## Кампус\n\nКампус университета расположен на берегу Волги.\n"
    passages = chunk_markdown(markdown, min_section_chars=10)

    assert passages[0].heading == "Иннополис > Кампус"


def test_passage_ids_are_sequential_kb_prefixed() -> None:
    markdown = (
        "## Раздел A\n\nA " * 20 + "\n\n## Раздел B\n\nB " * 20 + "\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=10)

    assert [p.id for p in passages] == [f"kb_{i:04d}" for i in range(len(passages))]


def test_short_section_glued_to_following_section() -> None:
    markdown = (
        "## Вступление\n\nКоротко.\n\n"
        "## Основной раздел\n\n"
        + "Здесь много текста про экскурсию по кампусу и его историю. " * 3
        + "\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=120)

    assert len(passages) == 1
    assert "Коротко." in passages[0].text
    assert passages[0].heading == "Основной раздел"


def test_short_trailing_section_glued_to_previous() -> None:
    markdown = (
        "## Основной раздел\n\n"
        + "Здесь много текста про экскурсию по кампусу и его историю. " * 3
        + "\n\n## Итог\n\nКоротко.\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=120)

    assert len(passages) == 1
    assert "Коротко." in passages[0].text
    assert passages[0].heading == "Основной раздел"


def test_long_section_split_by_paragraphs_with_one_paragraph_overlap() -> None:
    paragraph = "Абзац номер X про экспонат лаборатории роботов. " * 6  # ~300 символов
    markdown = "## Длинный раздел\n\n" + "\n\n".join(
        paragraph.replace("X", str(i)) for i in range(4)
    )
    passages = chunk_markdown(markdown, max_chars_per_passage=600, min_section_chars=10)

    assert len(passages) > 1
    for passage in passages:
        assert passage.heading == "Длинный раздел"
    # Перекрытие: последний абзац чанка N -- первый абзац чанка N+1.
    first_chunk_last_paragraph = passages[0].text.split("\n\n")[-1]
    second_chunk_first_paragraph = passages[1].text.split("\n\n")[0]
    assert first_chunk_last_paragraph == second_chunk_first_paragraph


def test_single_paragraph_longer_than_limit_is_not_split_mid_paragraph() -> None:
    huge_paragraph = "Слово " * 400  # длиннее 600 символов, один абзац
    markdown = f"## Раздел\n\n{huge_paragraph}\n"
    passages = chunk_markdown(markdown, max_chars_per_passage=600, min_section_chars=10)

    assert len(passages) == 1
    assert passages[0].text.strip() == huge_paragraph.strip()


def test_location_ids_parsed_from_leading_body_line() -> None:
    markdown = (
        "## Демонстрационная лаборатория\n\n"
        "location_ids: lab_demo, lab_demo_2\n\n"
        "Здесь стоят демонстрационные роботы и макеты для показа гостям университета.\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=10)

    assert passages[0].location_ids == ("lab_demo", "lab_demo_2")
    assert "location_ids:" not in passages[0].text


def test_location_ids_only_parent_section_merges_forward_without_leading_blank() -> None:
    """Регрессия: родительский заголовок, чьё тело -- только `location_ids:`, короче
    min_section_chars и клеится вперёд -- итоговый текст не должен начинаться с "\n\n"."""
    markdown = (
        "## Кампус\n\n"
        "location_ids: lab_demo\n\n"
        "### Демонстрационная лаборатория\n\n"
        "В демонстрационной лаборатории показывают действующие макеты и прототипы "
        "студенческих проектов, роботов и манипуляторов для гостей университета.\n"
    )
    passages = chunk_markdown(markdown, min_section_chars=120)

    assert len(passages) == 1
    assert passages[0].location_ids == ("lab_demo",)
    assert not passages[0].text.startswith("\n")
    assert not passages[0].text.startswith(" ")


def test_no_location_ids_line_leaves_body_untouched() -> None:
    markdown = "## Кафе\n\nЗдесь можно перекусить между остановками экскурсии по кампусу.\n"
    passages = chunk_markdown(markdown, min_section_chars=10)

    assert passages[0].location_ids == ()


def test_empty_markdown_produces_no_passages() -> None:
    assert chunk_markdown("") == []
