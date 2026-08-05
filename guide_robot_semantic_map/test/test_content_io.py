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


# -- pick_language -----------------------------------------------------------


def test_pick_language_requested_available() -> None:
    assert pick_language({"ru", "en"}, "en", default_language="ru") == "en"


def test_pick_language_falls_back_to_default() -> None:
    assert pick_language({"ru", "en"}, "fr", default_language="ru") == "ru"


def test_pick_language_empty_requested_uses_default() -> None:
    assert pick_language({"ru", "en"}, "", default_language="ru") == "ru"


def test_pick_language_nothing_available() -> None:
    assert pick_language({"de"}, "fr", default_language="ru") is None
