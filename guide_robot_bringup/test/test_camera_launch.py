"""Структурные тесты камерного запуска (Taiga #2) -- без камеры и без запуска.

Проверяют форму launch-файлов, а не живой процесс:
- `camera.launch.py` -- один узел `v4l2_camera` с нужными параметрами
  (проверяем реальным вызовом `generate_launch_description()`);
- `hardware.launch.py` -- включает `camera.launch.py`, декларирует
  `use_vision` (false по умолчанию) и пробрасывает `vision_enabled` в
  `llm.launch.py` (строковый контроль -- аргументы include'а в launch API
  не читаются после сборки без полного выполнения).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_LAUNCH = _HERE.parent / "launch"
CAMERA_LAUNCH = _LAUNCH / "camera.launch.py"
HARDWARE_LAUNCH = _LAUNCH / "hardware.launch.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _find_nodes(description, found: list) -> None:
    """Рекурсивно собрать все `launch_ros.actions.Node` из описания."""
    from launch_ros.actions import Node

    for entity in description.entities:
        if isinstance(entity, Node):
            found.append(entity)
        elif hasattr(entity, "entities"):
            _find_nodes(entity, found)


def test_camera_launch_exists() -> None:
    assert CAMERA_LAUNCH.is_file(), f"нет файла {CAMERA_LAUNCH}"


def test_camera_launch_node_shape() -> None:
    """camera.launch.py: ровно один v4l2_camera-узел `camera` с ключевыми параметрами.

    Пакет/имя читаем из name-mangled полей `launch_ros.actions.Node` (публичных
    свойств до выполнения действия нет); ключи параметров -- из исходника
    (значения параметров в несобранном описании -- launch-подстановки, читать их
    без выполнения действия нельзя).
    """
    module = _load("camera_launch", CAMERA_LAUNCH)
    description = module.generate_launch_description()
    nodes: list = []
    _find_nodes(description, nodes)

    camera_nodes = [n for n in nodes if getattr(n, "_Node__package", None) == "v4l2_camera"]
    assert len(camera_nodes) == 1, f"ожидали один v4l2_camera, нашли {len(camera_nodes)}"
    node = camera_nodes[0]
    assert getattr(node, "_Node__node_name", None) == "camera"
    assert getattr(node, "_Node__node_executable", None) == "v4l2_camera_node"

    text = CAMERA_LAUNCH.read_text(encoding="utf-8")
    for key in ("camera_device", "image_width", "image_height", "camera_name", "frame_id"):
        assert f'"{key}"' in text, f"ключ параметра {key} не найден в camera.launch.py"


def test_hardware_includes_camera_gated_by_use_vision() -> None:
    """hardware.launch.py: include camera.launch.py + аргумент use_vision (default false)."""
    text = HARDWARE_LAUNCH.read_text(encoding="utf-8")
    assert "camera.launch.py" in text
    assert "use_vision" in text
    # Гейт: IncludeLaunchDescription камеры под IfCondition(use_vision).
    assert "declare_use_vision" in text
    assert 'default_value="false"' in text


def test_hardware_passes_vision_enabled_to_llm() -> None:
    """hardware.launch.py -> llm.launch.py: проброс vision_enabled (AC #2)."""
    text = HARDWARE_LAUNCH.read_text(encoding="utf-8")
    assert "vision_enabled" in text


def test_llm_launch_declares_vision_enabled() -> None:
    """llm.launch.py: аргумент vision_enabled -> параметр vision.enabled dialog_agent'а."""
    llm_launch = Path(_HERE.parent.parent) / "guide_robot_llm" / "launch" / "llm.launch.py"
    assert llm_launch.is_file(), f"нет файла {llm_launch}"
    text = llm_launch.read_text(encoding="utf-8")
    assert "vision_enabled" in text
    assert "vision.enabled" in text


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
