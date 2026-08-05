"""Загрузка и валидация content/*.yaml -- текстов экспонатов (design.md §1.3, §2).

Инвариант, который держит весь content_server: этот модуль не порождает
текст. Он только читает то, что уже написано ревьюером, и проверяет
структурные инварианты. Если файла для экспоната/языка нет --
вызывающий код получает пустой результат, а не сгенерированную заглушку;
решение, что сказать посетителю в этом случае, принимает mission, не
семантическая карта.

Файл на диске -- content/<exhibit_id>.<language>.yaml, язык -- последний
сегмент имени файла перед расширением. Оба поля дублируются внутри
самого файла (exhibit_id, language) -- расхождение с именем файла считается
ошибкой данных: скорее всего файл скопировали и забыли поправить.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Chunk",
    "ContentError",
    "ExhibitContent",
    "load_content_dir",
    "load_content_file",
    "pick_language",
    "select_chunks",
]

VALID_LEVELS: frozenset[str] = frozenset(["short", "full"])
_MAX_SENTENCES_SOFT = 3
_SENTENCE_END = re.compile(r"[.!?…]+")


class ContentError(ValueError):
    """Ошибка формата или консистентности content/*.yaml."""


@dataclass(frozen=True)
class Chunk:
    """Один фрагмент текста экспоната."""

    id: str
    level: str
    text: str


@dataclass(frozen=True)
class ExhibitContent:
    """Разобранный и провалидированный content/<exhibit_id>.<language>.yaml."""

    exhibit_id: str
    language: str
    version: str
    title: str
    chunks: list[Chunk]
    reviewed_by: str | None
    reviewed_at: str | None


def load_content_file(path: str | Path) -> tuple[ExhibitContent, list[str]]:
    """Прочитать, разобрать и провалидировать один файл контента.

    Возвращает (контент, список WARN-сообщений) -- превышение мягкого
    лимита предложений не отказ (design.md §2), но и не должно молча
    теряться: вызывающий код решает, куда его отправить (лог/SystemEvent).
    """
    location = Path(path)
    document = _read_yaml(location)
    content = _parse_content(document, source=str(location))
    warnings = _check_filename_consistency(location, content) + _soft_warnings(content)
    return content, warnings


def load_content_dir(path: str | Path) -> tuple[dict[tuple[str, str], ExhibitContent], list[str]]:
    """Загрузить весь каталог content/*.yaml разом (design.md §1.3 -- целиком в память).

    Ключ результата -- (exhibit_id, language). Коллизия ключа между двумя
    файлами структурно невозможна: _check_filename_consistency требует,
    чтобы имя файла дословно совпадало с "<exhibit_id>.<language>.yaml",
    а два файла с одинаковым именем не могут существовать в одном каталоге.
    """
    directory = Path(path)
    items: dict[tuple[str, str], ExhibitContent] = {}
    warnings: list[str] = []
    for file_path in sorted(directory.glob("*.yaml")):
        content, file_warnings = load_content_file(file_path)
        items[(content.exhibit_id, content.language)] = content
        warnings.extend(file_warnings)
    return items, warnings


def select_chunks(content: ExhibitContent, mode: str) -> list[str]:
    """Отдать тексты чанков для запрошенного mode, в порядке файла (design.md §0.6).

    mode=short -- подмножество уровня short; mode=full -- все чанки.
    Не валидирует mode -- это делает узел на границе с ROS-сервисом,
    где значение приходит из чужого запроса.
    """
    if mode == "full":
        return [c.text for c in content.chunks]
    return [c.text for c in content.chunks if c.level == "short"]


def pick_language(available: set[str], requested: str, default_language: str) -> str | None:
    """Выбрать язык контента: запрошенный -> default_language -> отказ (design.md §1.3).

    None означает "ничего подходящего нет" -- молчаливая подмена языка
    запрещена, факт фолбэка логирует вызывающий код (SystemEvent WARN),
    эта функция только выбирает, не объясняет выбор.
    """
    if requested and requested in available:
        return requested
    if default_language in available:
        return default_language
    return None


# -- разбор одного файла -------------------------------------------------------


def _parse_content(document: dict[str, Any], *, source: str) -> ExhibitContent:
    if not isinstance(document, dict):
        raise ContentError(f"{source}: корневой объект должен быть отображением (mapping)")

    exhibit_id = _require_str(document, "exhibit_id", source)
    language = _require_str(document, "language", source)
    version = _require_str(document, "version", source)
    title = _require_str(document, "title", source)
    reviewed_by = document.get("reviewed_by")
    reviewed_at = document.get("reviewed_at")

    raw_chunks = document.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise ContentError(f"{source}: chunks должен быть непустым списком")

    chunks: list[Chunk] = []
    seen_ids: set[str] = set()
    for index, raw_chunk in enumerate(raw_chunks):
        chunk = _parse_chunk(raw_chunk, index, source)
        if chunk.id in seen_ids:
            raise ContentError(f"{source}: дублирующийся id чанка {chunk.id!r}")
        seen_ids.add(chunk.id)
        chunks.append(chunk)

    if not any(c.level == "short" for c in chunks):
        raise ContentError(f"{source}: нет ни одного чанка уровня short")

    return ExhibitContent(
        exhibit_id=exhibit_id,
        language=language,
        version=version,
        title=title,
        chunks=chunks,
        reviewed_by=reviewed_by if isinstance(reviewed_by, str) else None,
        reviewed_at=reviewed_at if isinstance(reviewed_at, str) else None,
    )


def _parse_chunk(raw: Any, index: int, source: str) -> Chunk:
    if not isinstance(raw, dict):
        raise ContentError(f"{source}: chunks[{index}] должен быть отображением")
    chunk_id = _require_str(raw, "id", source, where=f"chunks[{index}]")
    level = raw.get("level")
    if level not in VALID_LEVELS:
        raise ContentError(
            f"{source}: chunks[{index}].level={level!r} не входит в {sorted(VALID_LEVELS)}"
        )
    text = _require_str(raw, "text", source, where=f"chunks[{index}]")
    return Chunk(id=chunk_id, level=level, text=text)


def _check_filename_consistency(path: Path, content: ExhibitContent) -> list[str]:
    stem = path.name.removesuffix(".yaml")
    if "." not in stem:
        raise ContentError(f"{path}: имя файла должно быть <exhibit_id>.<language>.yaml")
    file_exhibit_id, file_language = stem.rsplit(".", 1)
    if file_exhibit_id != content.exhibit_id or file_language != content.language:
        raise ContentError(
            f"{path}: имя файла указывает на exhibit_id={file_exhibit_id!r}, "
            f"language={file_language!r}, а внутри файла "
            f"exhibit_id={content.exhibit_id!r}, language={content.language!r}"
        )
    return []


def _soft_warnings(content: ExhibitContent) -> list[str]:
    warnings: list[str] = []
    for chunk in content.chunks:
        sentence_count = len([s for s in _SENTENCE_END.split(chunk.text) if s.strip()])
        if sentence_count > _MAX_SENTENCES_SOFT:
            warnings.append(
                f"{content.exhibit_id}.{content.language}: чанк {chunk.id!r} содержит "
                f"{sentence_count} предложений (мягкий лимит {_MAX_SENTENCES_SOFT})"
            )
    return warnings


# -- общие хелперы -----------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContentError(f"{path}: не удалось прочитать файл: {exc}") from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ContentError(f"{path}: невалидный YAML: {exc}") from exc


def _require_str(raw: dict[str, Any], key: str, source: str, *, where: str | None = None) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        prefix = where or source
        raise ContentError(f"{prefix}.{key} обязателен и должен быть непустой строкой")
    return value
