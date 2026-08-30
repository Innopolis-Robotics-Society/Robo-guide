"""Юниты на парсинг и валидацию content/*.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from guide_robot_semantic_map.lib.content_io import (
    ContentError,
    load_content_dir,
    load_content_file,
    pick_language,
    select_chunk_ids,
    select_chunk_objects,
    select_chunks,
)


def _content_doc(
    exhibit_id: str = "kandinsky_viii", language: str = "ru", **overrides: object
) -> dict:
    base = {
        "exhibit_id": exhibit_id,
        "language": language,
        "version": "2026-08-04.1",
        "title": "Композиция VIII",
        "reviewed_by": "Evgenii Shlomov",
        "reviewed_at": "2026-08-04",
        "chunks": [
            {"id": "c1", "level": "short", "text": "Написана в 1923 году."},
            {"id": "c2", "level": "full", "text": "Полный текст с подробностями."},
        ],
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, name: str, doc: dict) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return path


# -- load_content_file: happy path ---------------------------------------------


def test_parses_valid_content(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, warnings = load_content_file(path)
    assert content.exhibit_id == "kandinsky_viii"
    assert content.language == "ru"
    assert content.version == "2026-08-04.1"
    assert len(content.chunks) == 2
    assert warnings == []


def test_reviewed_fields_optional(tmp_path: Path) -> None:
    doc = _content_doc()
    del doc["reviewed_by"]
    del doc["reviewed_at"]
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.reviewed_by is None
    assert content.reviewed_at is None


# -- обязательные инварианты (design.md §2) -------------------------------------


def test_rejects_empty_text(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": ""}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="text"):
        load_content_file(path)


def test_rejects_empty_version(tmp_path: Path) -> None:
    doc = _content_doc(version="")
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="version"):
        load_content_file(path)


def test_rejects_duplicate_chunk_ids(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {"id": "c1", "level": "short", "text": "Раз."},
            {"id": "c1", "level": "full", "text": "Два."},
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="дублирующийся id чанка"):
        load_content_file(path)


def test_requires_at_least_one_short_chunk(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "full", "text": "Только полный."}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="short"):
        load_content_file(path)


def test_rejects_invalid_level(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "medium", "text": "Текст."}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="level"):
        load_content_file(path)


def test_rejects_empty_chunks_list(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="непустым списком"):
        load_content_file(path)


def test_rejects_filename_language_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.en.yaml", _content_doc(language="ru"))
    with pytest.raises(ContentError, match="имя файла указывает"):
        load_content_file(path)


def test_rejects_filename_exhibit_id_mismatch(tmp_path: Path) -> None:
    path = _write(tmp_path, "wrong_id.ru.yaml", _content_doc(exhibit_id="kandinsky_viii"))
    with pytest.raises(ContentError, match="имя файла указывает"):
        load_content_file(path)


def test_rejects_malformed_filename(tmp_path: Path) -> None:
    path = _write(tmp_path, "no_language_segment.yaml", _content_doc())
    with pytest.raises(ContentError, match=r"exhibit_id.*language"):
        load_content_file(path)


# -- мягкое предупреждение о длине --------------------------------------------


def test_long_chunk_produces_warning_not_error(tmp_path: Path) -> None:
    long_text = "Раз. Два. Три. Четыре."  # 4 предложения > мягкого лимита 3
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": long_text}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, warnings = load_content_file(path)
    assert content.chunks[0].text == long_text
    assert any("c1" in w for w in warnings)


def test_short_chunk_no_warning(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": "Раз. Два."}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    _, warnings = load_content_file(path)
    assert warnings == []


# -- load_content_dir -----------------------------------------------------------


def test_load_content_dir_collects_all_files(tmp_path: Path) -> None:
    _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    _write(
        tmp_path, "kandinsky_viii.en.yaml", _content_doc(language="en", title="Composition VIII")
    )
    items, warnings = load_content_dir(tmp_path)
    assert set(items) == {("kandinsky_viii", "ru"), ("kandinsky_viii", "en")}
    assert warnings == []


def test_load_content_dir_propagates_single_file_error(tmp_path: Path) -> None:
    # Один битый файл -- весь каталог не грузится (design.md §1: любая
    # ошибка = FAILURE на configure, частичной загрузки не бывает).
    _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    _write(tmp_path, "other.ru.yaml", _content_doc(exhibit_id="other", version=""))
    with pytest.raises(ContentError, match="version"):
        load_content_dir(tmp_path)


def test_load_content_dir_empty_directory(tmp_path: Path) -> None:
    items, warnings = load_content_dir(tmp_path)
    assert items == {}
    assert warnings == []


# -- select_chunks ---------------------------------------------------------------


def test_select_chunks_short_mode_subset(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert select_chunks(content, "short") == ["Написана в 1923 году."]


def test_select_chunks_full_mode_everything(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert select_chunks(content, "full") == [
        "Написана в 1923 году.",
        "Полный текст с подробностями.",
    ]


def test_select_chunks_preserves_file_order(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {"id": "c1", "level": "full", "text": "Первый."},
            {"id": "c2", "level": "short", "text": "Второй."},
            {"id": "c3", "level": "short", "text": "Третий."},
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert select_chunks(content, "short") == ["Второй.", "Третий."]
    assert select_chunks(content, "full") == ["Первый.", "Второй.", "Третий."]


# -- select_chunk_ids -- параллельно select_chunks -------------------------------


def test_select_chunk_ids_matches_select_chunks_order(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {"id": "c1", "level": "full", "text": "Первый."},
            {"id": "c2", "level": "short", "text": "Второй."},
            {"id": "c3", "level": "short", "text": "Третий."},
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert select_chunk_ids(content, "short") == ["c2", "c3"]
    assert select_chunk_ids(content, "full") == ["c1", "c2", "c3"]
    assert len(select_chunk_ids(content, "full")) == len(select_chunks(content, "full"))


# -- pick_language -----------------------------------------------------------


def test_pick_language_requested_available() -> None:
    assert pick_language({"ru", "en"}, "en", default_language="ru") == "en"


def test_pick_language_falls_back_to_default() -> None:
    assert pick_language({"ru", "en"}, "fr", default_language="ru") == "ru"


def test_pick_language_empty_requested_uses_default() -> None:
    assert pick_language({"ru", "en"}, "", default_language="ru") == "ru"


def test_pick_language_nothing_available() -> None:
    assert pick_language({"de"}, "fr", default_language="ru") is None


# -- kind / location_ids (design.md §1.3, CLAUDE_CODE_TASK_stage1_knowledge.md §1) ----


def test_kind_defaults_to_exhibit(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert content.kind == "exhibit"


def test_kind_explicit_value_accepted(tmp_path: Path) -> None:
    doc = _content_doc(kind="place")
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.kind == "place"


def test_rejects_unknown_kind(tmp_path: Path) -> None:
    doc = _content_doc(kind="painting")
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="kind"):
        load_content_file(path)


def test_location_ids_defaults_to_exhibit_id_for_exhibit_kind(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert content.location_ids == ["kandinsky_viii"]


def test_location_ids_defaults_to_empty_for_non_exhibit_kind(tmp_path: Path) -> None:
    doc = _content_doc(kind="city")
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.location_ids == []


def test_location_ids_explicit_value_accepted(tmp_path: Path) -> None:
    doc = _content_doc(kind="place", location_ids=["entrance", "robo_guide"])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.location_ids == ["entrance", "robo_guide"]


def test_rejects_non_string_location_ids(tmp_path: Path) -> None:
    doc = _content_doc(location_ids=["entrance", 5])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="location_ids"):
        load_content_file(path)


def test_rejects_empty_string_location_id(tmp_path: Path) -> None:
    doc = _content_doc(location_ids=["entrance", ""])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="location_ids"):
        load_content_file(path)


# -- interruptible / pause_after_s (stage4 §1.1) ------------------------------


def test_chunk_interruptible_defaults_true(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert all(c.interruptible is True for c in content.chunks)


def test_chunk_pause_after_s_defaults_zero(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert all(c.pause_after_s == 0.0 for c in content.chunks)


def test_chunk_interruptible_and_pause_after_s_explicit_values(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {
                "id": "c1",
                "level": "short",
                "text": "Я добрый. Так написано в инструкции.",
                "interruptible": False,
                "pause_after_s": 1.2,
            }
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.chunks[0].interruptible is False
    assert content.chunks[0].pause_after_s == 1.2


def test_rejects_non_bool_interruptible(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": "Т.", "interruptible": 1}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="interruptible"):
        load_content_file(path)


def test_rejects_non_numeric_pause_after_s(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[{"id": "c1", "level": "short", "text": "Т.", "pause_after_s": "long"}]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="pause_after_s"):
        load_content_file(path)


def test_rejects_negative_pause_after_s(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": "Т.", "pause_after_s": -1}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="pause_after_s"):
        load_content_file(path)


def test_rejects_pause_after_s_above_max(tmp_path: Path) -> None:
    doc = _content_doc(chunks=[{"id": "c1", "level": "short", "text": "Т.", "pause_after_s": 16}])
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    with pytest.raises(ContentError, match="pause_after_s"):
        load_content_file(path)


def test_accepts_pause_after_s_at_bounds(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {"id": "c1", "level": "short", "text": "Т.", "pause_after_s": 0},
            {"id": "c2", "level": "full", "text": "Т.", "pause_after_s": 15},
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    assert content.chunks[0].pause_after_s == 0.0
    assert content.chunks[1].pause_after_s == 15.0


# -- select_chunk_objects -- полные чанки, не только текст --------------------


def test_select_chunk_objects_carries_interruptible_and_pause(tmp_path: Path) -> None:
    doc = _content_doc(
        chunks=[
            {
                "id": "c1",
                "level": "short",
                "text": "Раз.",
                "interruptible": False,
                "pause_after_s": 2.0,
            },
            {"id": "c2", "level": "full", "text": "Два."},
        ]
    )
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", doc)
    content, _ = load_content_file(path)
    objects = select_chunk_objects(content, "full")
    assert [o.text for o in objects] == ["Раз.", "Два."]
    assert objects[0].interruptible is False
    assert objects[0].pause_after_s == 2.0
    assert objects[1].interruptible is True
    assert objects[1].pause_after_s == 0.0


def test_select_chunk_objects_short_mode_matches_select_chunks(tmp_path: Path) -> None:
    path = _write(tmp_path, "kandinsky_viii.ru.yaml", _content_doc())
    content, _ = load_content_file(path)
    assert [o.text for o in select_chunk_objects(content, "short")] == select_chunks(
        content, "short"
    )
    assert [o.id for o in select_chunk_objects(content, "full")] == select_chunk_ids(
        content, "full"
    )
