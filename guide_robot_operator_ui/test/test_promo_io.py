"""Юниты lib/promo_io.py -- без rclpy, без ROS-графа (design F1/F2).

`load_promo` НИКОГДА не бросает -- опечатка в promo.yaml не должна валить
узел, только промо-петлю (design F, "Правила": "Контент промо -- не твоя
задача"). Это единственное, чем этот модуль сознательно отличается от
guide_robot_semantic_map's content_io.py, чью форму (`Media`-датакласс,
разбор одного файла) он иначе копирует.
"""

from __future__ import annotations

from pathlib import Path

from guide_robot_operator_ui.lib.promo_io import load_promo


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_missing_file_returns_empty_with_warning(tmp_path: Path) -> None:
    items, warnings = load_promo(tmp_path / "promo.yaml", tmp_path / "media")
    assert items == []
    assert len(warnings) == 1
    assert "не найден" in warnings[0]


def test_empty_items_returns_empty_without_error(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, 'version: "1"\nitems: []\n')
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert warnings == []


def test_malformed_yaml_returns_empty_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items: [this is not: valid: yaml\n")
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert len(warnings) == 1


def test_valid_manifest_parses_two_items(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "hall.jpg").write_bytes(b"fake")
    (media / "teaser.mp4").write_bytes(b"fake")
    promo_yaml = tmp_path / "promo.yaml"
    _write(
        promo_yaml,
        """
        version: "1"
        items:
          - {id: p1, kind: image, file: hall.jpg, duration_s: 6.0, caption: ""}
          - {id: p2, kind: video, file: teaser.mp4}
        """,
    )
    items, warnings = load_promo(promo_yaml, media)
    assert warnings == []
    assert [i.id for i in items] == ["p1", "p2"]
    assert items[0].kind == "image"
    assert items[0].duration_s == 6.0
    assert items[1].kind == "video"
    assert items[1].duration_s == 0.0


def test_duplicate_id_is_skipped_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(
        promo_yaml,
        """
        items:
          - {id: p1, kind: image, file: a.jpg}
          - {id: p1, kind: image, file: b.jpg}
        """,
    )
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert [i.id for i in items] == ["p1"]
    assert any("дублирующийся id" in w for w in warnings)


def test_unknown_kind_is_skipped_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items:\n  - {id: p1, kind: audio, file: a.mp3}\n")
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert any(".kind=" in w for w in warnings)


def test_duration_out_of_range_is_skipped_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items:\n  - {id: p1, kind: image, file: a.jpg, duration_s: -1.0}\n")
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert any("duration_s" in w for w in warnings)


def test_missing_id_is_skipped_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items:\n  - {kind: image, file: a.jpg}\n")
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert any(".id" in w for w in warnings)


def test_file_missing_on_disk_is_soft_warning_not_dropped(tmp_path: Path) -> None:
    """Мягкая проверка (design F1): item остаётся в списке, только WARN."""
    media = tmp_path / "media"
    media.mkdir()
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items:\n  - {id: p1, kind: image, file: nope.jpg}\n")
    items, warnings = load_promo(promo_yaml, media)
    assert [i.id for i in items] == ["p1"]
    assert any("не найден" in w for w in warnings)


def test_non_list_items_returns_empty_with_warning(tmp_path: Path) -> None:
    promo_yaml = tmp_path / "promo.yaml"
    _write(promo_yaml, "items: not-a-list\n")
    items, warnings = load_promo(promo_yaml, tmp_path / "media")
    assert items == []
    assert len(warnings) == 1
