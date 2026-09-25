"""config_tools: режим с одним лидаром убирает /scan_right из watchdog scan_rate."""

import copy
from pathlib import Path

import pytest
import yaml

from guide_robot_supervisor.config_tools import (
    without_scan_topic,
    write_config_without_scan_topic,
)

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
_CONFIGS = ["supervisor.yaml", "supervisor_slam.yaml"]


def _scan_topics(cfg):
    watchdogs = cfg["supervisor"]["watchdogs"]
    return next(w for w in watchdogs if w["name"] == "scan_rate")["params"]["topics"]


@pytest.mark.parametrize("name", _CONFIGS)
def test_real_configs_lose_only_scan_right(name):
    cfg = yaml.safe_load((_CONFIG_DIR / name).read_text())
    before = copy.deepcopy(cfg)

    patched = without_scan_topic(cfg, "/scan_right")

    assert _scan_topics(patched) == ["/scan", "/scan_left"]
    assert cfg == before  # исходный словарь не тронут
    # всё остальное (группы, остальные watchdog'и) без изменений
    assert patched["supervisor"]["groups"] == cfg["supervisor"]["groups"]
    assert len(patched["supervisor"]["watchdogs"]) == len(cfg["supervisor"]["watchdogs"])


@pytest.mark.parametrize("name", _CONFIGS)
def test_scan_rate_still_gates_safety(name):
    # Предусловие safety остаётся: мы сужаем набор топиков, а не выключаем проверку.
    patched = without_scan_topic(yaml.safe_load((_CONFIG_DIR / name).read_text()), "/scan_right")
    safety = next(g for g in patched["supervisor"]["groups"] if g["name"] == "safety")
    assert "scan_rate" in safety["preconditions"]


def test_missing_topic_is_a_noop():
    cfg = yaml.safe_load((_CONFIG_DIR / "supervisor.yaml").read_text())
    assert without_scan_topic(cfg, "/scan_nope") == cfg


def test_no_scan_rate_watchdog_raises():
    with pytest.raises(ValueError, match="scan_rate"):
        without_scan_topic({"supervisor": {"watchdogs": []}}, "/scan_right")


def test_removing_the_last_topic_raises():
    cfg = {
        "supervisor": {"watchdogs": [{"name": "scan_rate", "params": {"topics": ["/scan_right"]}}]}
    }
    with pytest.raises(ValueError, match="не осталось топиков"):
        without_scan_topic(cfg, "/scan_right")


def test_write_produces_a_loadable_file(tmp_path):
    dst = tmp_path / "out.yaml"
    result = write_config_without_scan_topic(
        str(_CONFIG_DIR / "supervisor.yaml"), str(dst), "/scan_right"
    )
    assert result == str(dst)
    loaded = yaml.safe_load(dst.read_text())
    assert _scan_topics(loaded) == ["/scan", "/scan_left"]
    # то, что читает supervisor_node: cfg["supervisor"]["groups"/"watchdogs"]
    assert {"groups", "watchdogs"} <= set(loaded["supervisor"])
