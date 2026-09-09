"""Загрузка и валидация promo/promo.yaml -- промо-петля в простое (design F1/F2).

Пуст/не найден/невалиден -- НЕ отказ узла, в отличие от
`guide_robot_semantic_map`'s `content_io.py`: промо не курируется и не
ревьюится ("Контент промо -- не твоя задача", design F, "Правила"),
поэтому опечатка в `promo.yaml` не должна валить узел или брать вниз
панель управления -- пустой манифест, WARN, клиент сам покажет статичную
заставку (design F3, критерий 5).

Существование файла на диске -- тоже мягкая проверка (строка в
`warnings`, не исключение), той же причины ради: битый путь всё равно
ловится на клиенте через тот же брокен-медиа путь, что и слайды тура
(design D5/F3, критерий 6), а отказ узла из-за забытого файла -- цена
выше пользы.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["PromoItem", "load_promo"]

_VALID_KINDS = frozenset(["image", "video"])
_MAX_DURATION_S = 600.0


@dataclass(frozen=True)
class PromoItem:
    """Один элемент промо-петли -- форма `Media` из semantic_map's content_io.py.

    Без `chunk_id` (design F1: привязывать не к чему).
    """

    id: str
    kind: str
    file: str
    duration_s: float = 0.0
    caption: str = ""


def load_promo(promo_yaml: Path, media_root: Path) -> tuple[list[PromoItem], list[str]]:
    """Прочитать promo.yaml; вернуть (items, warnings). Никогда не бросает.

    `media_root` -- каталог promo/media/, только для мягкой (WARN, не
    отказ) проверки, что файлы items на месте.
    """
    if not promo_yaml.is_file():
        return [], [f"{promo_yaml}: не найден -- промо-петля пуста"]

    try:
        raw = promo_yaml.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"{promo_yaml}: не прочитан ({exc}) -- промо-петля пуста"]

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return [], [f"{promo_yaml}: невалидный YAML ({exc}) -- промо-петля пуста"]

    if not isinstance(document, dict):
        return [], [f"{promo_yaml}: корневой объект должен быть отображением (mapping)"]

    raw_items = document.get("items", [])
    if not isinstance(raw_items, list):
        return [], [f"{promo_yaml}: items должен быть списком"]

    items: list[PromoItem] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item, item_warnings = _parse_item(raw_item, index, promo_yaml)
        warnings.extend(item_warnings)
        if item is None:
            continue
        if item.id in seen_ids:
            warnings.append(
                f"{promo_yaml}: items[{index}] -- дублирующийся id {item.id!r}, пропущен"
            )
            continue
        seen_ids.add(item.id)
        if not (media_root / item.file).is_file():
            warnings.append(
                f"{promo_yaml}: items[{index}].file={item.file!r} не найден "
                f"({media_root / item.file})"
            )
        items.append(item)

    return items, warnings


def _parse_item(raw: Any, index: int, source: Path) -> tuple[PromoItem | None, list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{source}: items[{index}] должен быть отображением, пропущен"]

    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None, [
            f"{source}: items[{index}].id обязателен и должен быть непустой строкой, пропущен"
        ]

    kind = raw.get("kind")
    if kind not in _VALID_KINDS:
        return None, [
            f"{source}: items[{index}].kind={kind!r} не входит в {sorted(_VALID_KINDS)}, пропущен"
        ]

    file = raw.get("file")
    if not isinstance(file, str) or not file:
        return None, [
            f"{source}: items[{index}].file обязателен и должен быть непустой строкой, пропущен"
        ]

    duration_s = raw.get("duration_s", 0.0)
    if isinstance(duration_s, bool) or not isinstance(duration_s, int | float):
        return None, [f"{source}: items[{index}].duration_s должен быть числом, пропущен"]
    duration_s = float(duration_s)
    if not 0.0 <= duration_s <= _MAX_DURATION_S:
        return None, [
            f"{source}: items[{index}].duration_s={duration_s} вне диапазона "
            f"[0, {_MAX_DURATION_S}], пропущен"
        ]

    caption = raw.get("caption", "")
    if not isinstance(caption, str):
        return None, [f"{source}: items[{index}].caption должен быть строкой, пропущен"]

    return (
        PromoItem(id=item_id, kind=kind, file=file, duration_s=duration_s, caption=caption),
        [],
    )
