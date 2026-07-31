"""Смоук-тесты командной строки measure_t_stop.

Существуют из-за реальной регрессии: аргумент --allow-shared был добавлен
в обращения args.allow_shared, но не в парсер. Ни ruff, ни pytest этого
не видели -- падало только в рантайме, на железе, у человека.
"""

from __future__ import annotations

import inspect
import re

import pytest

from guide_robot_voice.tools import measure_t_stop


def test_defaults_match_usb_audio_class() -> None:
    """Умолчания соответствуют тому, что умеет типичный USB Audio Class."""
    args = measure_t_stop.build_parser().parse_args([])
    assert args.sample_rate == 48000
    assert args.channels == 2
    assert args.allow_shared is False


def test_every_referenced_attribute_is_declared() -> None:
    """Все обращения args.X в модуле объявлены в парсере.

    Ровно тот класс ошибки, который дал AttributeError в рантайме.
    """
    source = inspect.getsource(measure_t_stop)
    referenced = set(re.findall(r"\bargs\.(\w+)", source))
    declared = vars(measure_t_stop.build_parser().parse_args([])).keys()
    assert referenced <= set(declared), f"не объявлены: {referenced - set(declared)}"


@pytest.mark.parametrize(
    "argv",
    [
        ["--check", "--device", "hw:2,0"],
        ["--acoustic", "--device", "hw:2,0", "--sample-rate", "48000"],
        ["--list"],
        ["--allow-shared", "--device", "pulse"],
    ],
)
def test_documented_invocations_parse(argv: list[str]) -> None:
    """Команды, которые встречаются в документации, разбираются."""
    measure_t_stop.build_parser().parse_args(argv)


def test_help_exits_zero() -> None:
    """--help не падает на импорте: модуль грузится без звуковой карты."""
    with pytest.raises(SystemExit) as excinfo:
        measure_t_stop.main(["--help"])
    assert excinfo.value.code == 0
