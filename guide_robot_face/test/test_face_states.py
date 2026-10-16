"""Проверка face_states.yaml -- без aiohttp и без ROS."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_REQUIRED_STATE_KEYS = {"w", "h", "rr", "rot", "curve", "gx", "gy"}
_STATES_YAML = Path(__file__).resolve().parent.parent / "config" / "face_states.yaml"


def _load_yaml() -> dict:
    return yaml.safe_load(_STATES_YAML.read_text(encoding="utf-8"))


def test_face_states_yaml_has_required_keys() -> None:
    doc = _load_yaml()
    assert doc["global"]["view_w"] == 1024
    assert doc["global"]["view_h"] == 768
    assert "idle" in doc["states"]
    for name, params in doc["states"].items():
        missing = _REQUIRED_STATE_KEYS - set(params)
        assert not missing, f"{name}: missing {missing}"


def test_speaking_and_thinking_are_alert() -> None:
    doc = _load_yaml()
    idle = doc["states"]["idle"]
    for name in ("speaking", "thinking"):
        s = doc["states"][name]
        assert s["curve"] <= 0.05, f"{name}: нижнее веко не должно висеть"
        assert s["h"] >= idle["h"], f"{name}: глаза не ниже idle"
        assert s["gy"] <= 0, f"{name}: взгляд не вниз"
    thinking = doc["states"]["thinking"]
    assert thinking["gx"] == 0
    assert thinking["rot"] == 0
    listening = doc["states"]["listening"]
    assert listening["w"] > idle["w"]
    assert listening["h"] > idle["h"]


def test_no_sad_state() -> None:
    assert "sad" not in _load_yaml()["states"]


def test_themes_light_is_black_on_white() -> None:
    g = _load_yaml()["global"]
    assert g["theme"] in g["themes"]
    assert g["themes"]["light"] == {"eye_color": "#000000", "bg_color": "#ffffff"}
    assert set(g["themes"]["dark"]) == {"eye_color", "bg_color"}


def test_web_states_json_matches_yaml() -> None:
    json_path = Path(__file__).resolve().parent.parent / "web" / "states.json"
    assert json.loads(json_path.read_text(encoding="utf-8")) == _load_yaml()
