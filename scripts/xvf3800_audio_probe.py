#!/usr/bin/env python3
"""Диагностика ALSA-тракта ReSpeaker XVF3800 без ROS 2.

По умолчанию скрипт только читает состояние USB/ALSA. Запись и воспроизведение
запускаются исключительно явными флагами, чтобы случайный диагностический
запуск не дал звук в усилитель.
"""

from __future__ import annotations

import argparse
import glob
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

_DEVICE_LINE = re.compile(
    r"^card\s+(?P<card_index>\d+):\s+"
    r"(?P<card_id>[^\s]+)\s+\[(?P<card_name>.*?)\],\s+"
    r"device\s+(?P<device_index>\d+):\s+"
    r"(?P<device_name>.*?)(?:\s+\[.*)?$"
)
_XVF_NAME = re.compile(r"xvf[\s_-]*3800|respeaker(?:[\s_-]+3800)?", re.IGNORECASE)
_TEST_AMPLITUDE = 1800  # около -25 dBFS: слышно, но безопаснее полного уровня.


@dataclass(frozen=True)
class Endpoint:
    """Один hardware PCM из вывода arecord/aplay -l."""

    card_index: int
    card_id: str
    card_name: str
    device_index: int
    device_name: str

    @property
    def hw_name(self) -> str:
        """Вернуть стабильное ALSA-имя по card id, а не по номеру карты."""
        return f"hw:CARD={self.card_id},DEV={self.device_index}"

    @property
    def plughw_name(self) -> str:
        """Вернуть ALSA-имя с явным plug-конвертером формата."""
        return f"plughw:CARD={self.card_id},DEV={self.device_index}"


def parse_device_list(output: str) -> list[Endpoint]:
    """Разобрать машинно значимые строки из ``arecord/aplay -l``."""
    endpoints: list[Endpoint] = []
    for raw_line in output.splitlines():
        match = _DEVICE_LINE.match(raw_line.strip())
        if not match:
            continue
        values = match.groupdict()
        endpoints.append(
            Endpoint(
                card_index=int(values["card_index"]),
                card_id=values["card_id"],
                card_name=values["card_name"],
                device_index=int(values["device_index"]),
                device_name=values["device_name"].strip(),
            )
        )
    return endpoints


def is_xvf3800(endpoint: Endpoint) -> bool:
    """Проверить endpoint по всем именам, которые сообщает ALSA."""
    haystack = " ".join((endpoint.card_id, endpoint.card_name, endpoint.device_name))
    return bool(_XVF_NAME.search(haystack))


def parse_udev_properties(output: str) -> dict[str, str]:
    """Разобрать вывод ``udevadm info --query=property``."""
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def read_card_properties(card_index: int) -> dict[str, str]:
    """Прочитать USB/udev-идентичность ALSA-карты."""
    if shutil.which("udevadm") is None:
        return {}
    result = subprocess.run(
        [
            "udevadm",
            "info",
            "--query=property",
            f"--path=/sys/class/sound/card{card_index}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return parse_udev_properties(result.stdout) if result.returncode == 0 else {}


def select_endpoint(
    endpoints: list[Endpoint],
    card: str | None,
    serial: str | None = None,
    properties_by_card: dict[int, dict[str, str]] | None = None,
) -> Endpoint | None:
    """Выбрать XVF3800 endpoint по имени карты и USB serial."""
    matches = [endpoint for endpoint in endpoints if is_xvf3800(endpoint)]
    if card is not None:
        matches = [
            endpoint
            for endpoint in matches
            if endpoint.card_id.casefold() == card.casefold() or str(endpoint.card_index) == card
        ]
    if serial is not None:
        properties_by_card = properties_by_card or {}
        matches = [
            endpoint
            for endpoint in matches
            if properties_by_card.get(endpoint.card_index, {}).get("ID_SERIAL_SHORT") == serial
        ]
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(endpoint.hw_name for endpoint in matches)
        raise RuntimeError(f"найдено несколько XVF3800 endpoint: {names}; задайте --card")
    return matches[0]


def run_listing(command: str) -> tuple[str, str, int]:
    """Запустить диагностическую команду, сохранив stdout, stderr и код."""
    if shutil.which(command) is None:
        return "", f"команда {command!r} не установлена", 127
    result = subprocess.run(
        [command, "-l"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


def print_section(title: str, text: str) -> None:
    """Напечатать читаемый раздел отчёта."""
    print(f"\n== {title} ==")
    print(text.rstrip() or "(нет данных)")


def print_usb_devices() -> None:
    """Показать USB enumeration, если lsusb доступен."""
    if shutil.which("lsusb") is None:
        print_section("USB", "команда 'lsusb' не установлена")
        return
    result = subprocess.run(["lsusb"], check=False, capture_output=True, text=True)
    text = result.stdout if result.returncode == 0 else result.stderr
    print_section("USB", text)


def print_stream_descriptors(card_index: int) -> None:
    """Показать форматы UAC прямо из procfs, не открывая PCM."""
    paths = sorted(glob.glob(f"/proc/asound/card{card_index}/stream*"))
    if not paths:
        print_section("UAC descriptors", "в /proc/asound нет stream-дескрипторов")
        return
    for path_text in paths:
        path = Path(path_text)
        try:
            contents = path.read_text(encoding="utf-8")
        except OSError as error:
            contents = f"не удалось прочитать: {error}"
        print_section(str(path), contents)


def print_mixer_controls(card_index: int) -> None:
    """Показать mixer controls без изменения громкости или mute."""
    if shutil.which("amixer") is None:
        print_section("ALSA mixer", "команда 'amixer' не установлена")
        return
    result = subprocess.run(
        ["amixer", "-c", str(card_index), "scontrols"],
        check=False,
        capture_output=True,
        text=True,
    )
    text = result.stdout if result.returncode == 0 else result.stderr
    print_section("ALSA mixer controls", text)


def print_device_identity(properties: dict[str, str]) -> None:
    """Показать идентификаторы конкретного физического USB-устройства."""
    serial = properties.get("ID_SERIAL_SHORT", "не предоставлен устройством")
    vendor_id = properties.get("ID_VENDOR_ID", "?")
    model_id = properties.get("ID_MODEL_ID", "?")
    revision = properties.get("ID_REVISION", "?")
    print("\nUSB identity:")
    print(f"  VID:PID:  {vendor_id}:{model_id}")
    print(f"  serial:   {serial}")
    print(f"  revision: {revision}")


def write_stereo_test_tone(path: Path, rate: int) -> None:
    """Создать тихий WAV: сначала левый канал 440 Гц, затем правый 660 Гц."""
    segment_s = 0.7
    gap_s = 0.25
    samples = array("h")

    def append_segment(frequency: float, left: bool, duration: float) -> None:
        frames = round(rate * duration)
        for index in range(frames):
            value = round(_TEST_AMPLITUDE * math.sin(2.0 * math.pi * frequency * index / rate))
            samples.extend((value, 0) if left else (0, value))

    append_segment(440.0, True, segment_s)
    samples.extend((0, 0) * round(rate * gap_s))
    append_segment(660.0, False, segment_s)

    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


def analyse_wav(path: Path) -> list[tuple[float, float]]:
    """Вернуть (RMS dBFS, peak dBFS) для каждого канала 16-bit WAV."""
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError(f"ожидался 16-bit WAV, получено {source.getsampwidth() * 8} bit")
        channels = source.getnchannels()
        values = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        values.byteswap()

    result: list[tuple[float, float]] = []
    for channel in range(channels):
        channel_values = values[channel::channels]
        if not channel_values:
            result.append((float("-inf"), float("-inf")))
            continue
        peak = max(abs(value) for value in channel_values)
        mean_square = sum(value * value for value in channel_values) / len(channel_values)
        rms = math.sqrt(mean_square)
        rms_dbfs = 20.0 * math.log10(rms / 32768.0) if rms else float("-inf")
        peak_dbfs = 20.0 * math.log10(peak / 32768.0) if peak else float("-inf")
        result.append((rms_dbfs, peak_dbfs))
    return result


def record(endpoint: Endpoint, path: Path, rate: int, seconds: int) -> None:
    """Записать двухканальный processed stream в WAV."""
    command = [
        "arecord",
        "-D",
        endpoint.hw_name,
        "-f",
        "S16_LE",
        "-c",
        "2",
        "-r",
        str(rate),
        "-d",
        str(seconds),
        "-t",
        "wav",
        str(path),
    ]
    print(f"Запись: {' '.join(command)}")
    subprocess.run(command, check=True)


def print_wav_levels(path: Path) -> None:
    """Напечатать уровни каналов записанного WAV."""
    print(f"\nЗаписано: {path.resolve()}")
    for channel, (rms_dbfs, peak_dbfs) in enumerate(analyse_wav(path)):
        # UAC-дескриптор сообщает только FL/FR. Conference/ASR -- ожидаемая
        # раскладка штатной 2-channel прошивки, которую ещё надо подтвердить
        # прослушиванием и full-duplex тестом конкретной платы.
        label = (
            "ожидается Conference"
            if channel == 0
            else "ожидается ASR"
            if channel == 1
            else "неизвестен"
        )
        print(f"  channel {channel} ({label}): RMS {rms_dbfs:.1f} dBFS, peak {peak_dbfs:.1f} dBFS")


def play_test(endpoint: Endpoint, rate: int) -> None:
    """Проиграть безопасный поканальный тест в XVF3800."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as temporary:
        path = Path(temporary.name)
        write_stereo_test_tone(path, rate)
        print("Воспроизведение: левый канал 440 Гц, пауза, правый канал 660 Гц")
        subprocess.run(["aplay", "-D", endpoint.hw_name, str(path)], check=True)


def full_duplex_test(
    capture: Endpoint,
    playback: Endpoint,
    path: Path,
    rate: int,
    seconds: int,
) -> None:
    """Одновременно открыть capture и playback и сохранить результат AEC."""
    command = [
        "arecord",
        "-D",
        capture.hw_name,
        "-f",
        "S16_LE",
        "-c",
        "2",
        "-r",
        str(rate),
        "-d",
        str(seconds),
        "-t",
        "wav",
        str(path),
    ]
    print(f"Одновременная запись: {' '.join(command)}")
    process = subprocess.Popen(command)
    try:
        time.sleep(0.5)
        play_test(playback, rate)
        return_code = process.wait(timeout=seconds + 2)
    except BaseException:
        process.terminate()
        process.wait(timeout=2)
        raise
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)


def ensure_new_output(path: Path) -> None:
    """Не позволить тесту молча перезаписать существующую запись."""
    if path.exists():
        raise FileExistsError(f"файл уже существует: {path}; выберите другое имя")
    path.parent.mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Создать CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", help="ALSA card id или номер, если найдено несколько плат")
    parser.add_argument(
        "--serial",
        help="ожидаемый USB serial; завершить проверку ошибкой, если такой платы нет",
    )
    parser.add_argument("--rate", type=int, default=16000, help="частота теста (default: 16000)")
    parser.add_argument("--seconds", type=int, default=5, help="длительность записи (default: 5)")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--record", type=Path, metavar="WAV", help="записать processed stereo")
    actions.add_argument(
        "--playback-test",
        action="store_true",
        help="подать тихие тестовые тоны на левый и правый выход",
    )
    actions.add_argument(
        "--full-duplex",
        type=Path,
        metavar="WAV",
        help="одновременно воспроизвести тест и записать processed stereo",
    )
    return parser


def main() -> int:
    """Выполнить обнаружение и выбранный активный тест."""
    args = build_parser().parse_args()
    if args.rate <= 0 or args.seconds <= 0:
        raise SystemExit("--rate и --seconds должны быть положительными")

    print_usb_devices()
    capture_text, capture_error, capture_code = run_listing("arecord")
    playback_text, playback_error, playback_code = run_listing("aplay")
    print_section("ALSA capture", capture_text or capture_error)
    print_section("ALSA playback", playback_text or playback_error)

    if capture_code != 0 or playback_code != 0:
        print("\nНе удалось прочитать список ALSA-устройств.", file=sys.stderr)
        return 2

    capture_endpoints = parse_device_list(capture_text)
    playback_endpoints = parse_device_list(playback_text)
    card_indexes = {
        endpoint.card_index
        for endpoint in capture_endpoints + playback_endpoints
        if is_xvf3800(endpoint)
    }
    properties_by_card = {index: read_card_properties(index) for index in card_indexes}

    try:
        capture = select_endpoint(
            capture_endpoints,
            args.card,
            args.serial,
            properties_by_card,
        )
        playback = select_endpoint(
            playback_endpoints,
            args.card,
            args.serial,
            properties_by_card,
        )
    except RuntimeError as error:
        print(f"\n{error}", file=sys.stderr)
        return 2

    if capture is None or playback is None:
        missing = []
        if capture is None:
            missing.append("capture")
        if playback is None:
            missing.append("playback")
        expected = f" с USB serial {args.serial!r}" if args.serial else ""
        print(
            "\nXVF3800"
            + expected
            + " не найден как "
            + " и ".join(missing)
            + ". Проверьте USB-C порт XMOS рядом с 3,5-мм разъёмом, "
            "data-кабель и USB-прошивку платы.",
            file=sys.stderr,
        )
        return 3

    if capture.card_index != playback.card_index:
        print("\nCapture и playback найдены на разных ALSA-картах.", file=sys.stderr)
        return 3

    print("\nXVF3800 найден:")
    print(f"  card id:  {capture.card_id} (текущий номер {capture.card_index})")
    print(f"  capture:  {capture.hw_name}")
    print(f"  playback: {playback.hw_name}")
    print("  для конфигурации используем card id; номер card N может измениться после reboot")
    print_device_identity(properties_by_card.get(capture.card_index, {}))
    print_stream_descriptors(capture.card_index)
    print_mixer_controls(capture.card_index)

    try:
        if args.record is not None:
            ensure_new_output(args.record)
            record(capture, args.record, args.rate, args.seconds)
            print_wav_levels(args.record)
        elif args.playback_test:
            play_test(playback, args.rate)
        elif args.full_duplex is not None:
            ensure_new_output(args.full_duplex)
            full_duplex_test(capture, playback, args.full_duplex, args.rate, args.seconds)
            print_wav_levels(args.full_duplex)
    except (FileExistsError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"\nТест не выполнен: {error}", file=sys.stderr)
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
