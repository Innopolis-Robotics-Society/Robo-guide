"""Разрешение аудиоустройств по имени с внятными ошибками.

Вынесено в отдельный модуль после реального отказа: имя устройства
резолвилось лениво, внутри писательского потока, исключение терялось
в потоке, и измерительный инструмент напечатал результат при мёртвом
выводе. Ноль сэмплов прозвучало, метрика зелёная.

Правила модуля:

  * резолв происходит в вызывающем потоке, до старта чего бы то ни было;
  * ошибка перечисляет доступные устройства нужного направления;
  * отдельно различаются три случая, потому что чинятся они по-разному:
    имя не найдено, имя неоднозначно, устройство найдено но не имеет
    каналов в нужную сторону.

Третий случай -- не экзотика. Аналоговый кодек ноутбука при работающем
звуковом сервере отдаёт PortAudio только capture: playback-PCM уже занят.
Выглядит как "0 out" в списке и как загадочный отказ открытия, если
не сказать об этом прямо.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["Direction", "describe_devices", "resolve_device"]

Direction = Literal["input", "output"]

_CHANNEL_KEY = {"input": "max_input_channels", "output": "max_output_channels"}
_HUMAN = {"input": "захвата", "output": "вывода"}


def _query() -> list[dict]:
    import sounddevice as sd

    return list(sd.query_devices())


def describe_devices(direction: Direction | None = None) -> str:
    """Собрать читаемый список устройств для логов и текстов ошибок."""
    lines: list[str] = []
    for index, device in enumerate(_query()):
        ins = device["max_input_channels"]
        outs = device["max_output_channels"]
        if direction == "input" and not ins:
            continue
        if direction == "output" and not outs:
            continue
        lines.append(f"  [{index}] {device['name']}  (in={ins}, out={outs})")
    return "\n".join(lines) if lines else "  (нет подходящих устройств)"


def resolve_device(
    spec: str | int | None,
    direction: Direction,
    min_channels: int = 1,
) -> int | None:
    """Найти индекс устройства по спецификации.

    spec может быть None (устройство по умолчанию), целым индексом,
    строкой с индексом или подстрокой имени. Подстрока сравнивается
    с полным именем PortAudio, которое включает ALSA-идентификатор,
    поэтому "hw:1,0" и "pulse" работают наравне с именем вендора.
    """
    if spec is None or spec == "":
        return None

    devices = _query()
    key = _CHANNEL_KEY[direction]

    if isinstance(spec, int) or (isinstance(spec, str) and spec.lstrip("-").isdigit()):
        index = int(spec)
        if not 0 <= index < len(devices):
            raise ValueError(
                f"индекс устройства {index} вне диапазона 0..{len(devices) - 1}.\n"
                f"Устройства {_HUMAN[direction]}:\n{describe_devices(direction)}"
            )
        if devices[index][key] < min_channels:
            raise ValueError(_no_channels_message(index, devices[index], direction, min_channels))
        return index

    fragment = str(spec).lower()
    matches = [(i, d) for i, d in enumerate(devices) if fragment in d["name"].lower()]

    if not matches:
        raise ValueError(
            f"нет устройства {_HUMAN[direction]} с именем ~{spec!r}.\n"
            f"Доступны:\n{describe_devices(direction)}"
        )

    usable = [(i, d) for i, d in matches if d[key] >= min_channels]

    if not usable:
        index, device = matches[0]
        raise ValueError(_no_channels_message(index, device, direction, min_channels))

    if len(usable) > 1:
        listing = "\n".join(f"  [{i}] {d['name']}" for i, d in usable)
        raise ValueError(
            f"имя {spec!r} неоднозначно, подходит {len(usable)} устройств:\n{listing}\n"
            f"Уточните подстроку или задайте индекс."
        )

    return usable[0][0]


def _no_channels_message(
    index: int,
    device: dict,
    direction: Direction,
    min_channels: int,
) -> str:
    available = device[_CHANNEL_KEY[direction]]
    text = (
        f"устройство [{index}] {device['name']!r} найдено, но имеет "
        f"{available} каналов {_HUMAN[direction]} (нужно {min_channels})."
    )
    if direction == "output" and available == 0 and "hw:" in device["name"]:
        text += (
            "\nНа аналоговом кодеке ноутбука это обычно значит, что playback-PCM "
            "занят звуковым сервером. Варианты: использовать 'pulse'/'default', "
            "либо освободить устройство (pactl suspend-sink <sink> 1) и открыть hw: "
            "эксклюзивно."
        )
    text += f"\nУстройства {_HUMAN[direction]}:\n{describe_devices(direction)}"
    return text
