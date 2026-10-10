#!/usr/bin/env python3
"""Интерактивно собрать wake-word датасет из обработанного ROS-топика XVF3800.

Внутри контейнера, после source /home/fabian/ros2_ws/install/setup.bash:
  python3 /home/fabian/ros2_ws/src/scripts/record_wakeword_dataset.py positive --speaker s01
  python3 /home/fabian/ros2_ws/src/scripts/record_wakeword_dataset.py negative --condition crowd

Новая сессия выбирается автоматически. Для продолжения прерванной записи:
  ... positive --speaker s01 --session d01 --resume
Перед каждым дублем нажмите только Enter. WAV сохраняется автоматически.
Говорите после подсказки «ГОВОРИ: Фирая». Остановка — Ctrl+C.
Качество звука не фильтруется: разрывы, клиппинг и тишина не мешают сохранению.
--dry-run показывает план без ROS и без создания файлов.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "datasets/wakeword/firaya_v1/raw"
POSITIVE_PLAN = (
    ("quiet_1m_front", "Тихо, 1 метр перед роботом", 20),
    ("quiet_3m_front", "Тихо, 3 метра перед роботом", 15),
    ("crowd_2m_front", "Разговоры вокруг, 2 метра перед роботом; фон БЕЗ имени", 15),
)
NEGATIVE_CONDITIONS = (
    "crowd",
    "conversation",
    "robot_noise",
    "robot_tts",
    "similar_speech",
    "room_noise",
)


@dataclass(frozen=True)
class Take:
    """Один запланированный дубль."""

    condition: str
    hint: str
    number: int

    @property
    def filename(self) -> str:
        """Вернуть имя WAV внутри сессии."""
        return f"{self.condition}_{self.number:02d}.wav"


class CaptureError(RuntimeError):
    """Нет аудиоданных для записи; подключение можно повторить."""


def speaker_split(speaker: str) -> str:
    """Разделить первых 12 людей без перемешивания голосов между выборками."""
    number = int(speaker[1:])
    if number <= 8:
        return "train"
    if number <= 10:
        return "val"
    if number <= 12:
        return "test"
    raise ValueError("для s13 и далее задайте --split train, val или test явно")


def next_session(parent: Path) -> str:
    """Найти следующий номер, не используя повторно существующие сессии."""
    numbers = []
    if parent.exists():
        numbers = [
            int(path.name[1:])
            for path in parent.iterdir()
            if re.fullmatch(r"d[0-9]{2,}", path.name)
        ]
    return f"d{max(numbers, default=0) + 1:02d}"


def build_plan(arguments: argparse.Namespace) -> list[Take]:
    """Сформировать 50 положительных или заданное число отрицательных дублей."""
    if arguments.kind == "positive":
        return [
            Take(condition, hint, number)
            for condition, hint, count in POSITIVE_PLAN
            for number in range(1, count + 1)
        ]
    return [
        Take(arguments.condition, "Фон БЕЗ слова Фирая", number)
        for number in range(1, arguments.takes + 1)
    ]


def session_path(arguments: argparse.Namespace) -> Path:
    """Проверить параметры, отсутствие утечки голосов и выбрать папку сессии."""
    if arguments.resume and not arguments.session:
        raise ValueError("для --resume нужно указать --session d01 (или другой номер)")
    if arguments.session and not re.fullmatch(r"d[0-9]{2,}", arguments.session):
        raise ValueError("сессия должна иметь вид d01, d02, ...")
    if not math.isfinite(arguments.seconds) or arguments.seconds <= 0:
        raise ValueError("--seconds должен быть положительным конечным числом")
    if arguments.kind == "positive":
        if not re.fullmatch(r"s[0-9]{2,}", arguments.speaker) or int(arguments.speaker[1:]) < 1:
            raise ValueError("человек должен иметь номер s01, s02, ...")
        arguments.split = arguments.split or speaker_split(arguments.speaker)
        for split in ("train", "val", "test"):
            other = arguments.root / "positive" / split / arguments.speaker
            if split != arguments.split and other.exists():
                raise ValueError(f"{arguments.speaker} уже есть в {split}; нельзя менять выборку")
        parent = arguments.root / "positive" / arguments.split / arguments.speaker
    else:
        if arguments.takes < 1:
            raise ValueError("--takes должен быть больше нуля")
        parent = arguments.root / "negative" / arguments.split
    arguments.session = arguments.session or next_session(parent)
    path = parent / arguments.session
    if arguments.resume and not path.is_dir():
        raise ValueError(f"сессия для продолжения не найдена: {path}")
    if not arguments.resume and path.exists():
        raise ValueError(f"сессия уже существует: {path}; используйте --resume или новую сессию")
    return path


def _capture(topic: str, seconds: float, cue: str) -> tuple[bytes, dict]:
    """Собрать ROS-аудио без фильтрации качества, вывести метрики для справки."""
    try:
        import numpy as np
        import rclpy

        # Позволяет запускать как напрямую, так и через python -m scripts.…
        if __package__:
            from .xvf3800_ros_channel_probe import ChannelProbe, analyse_interleaved
        else:
            from xvf3800_ros_channel_probe import ChannelProbe, analyse_interleaved
    except ImportError as error:
        raise RuntimeError(
            "нет ROS-зависимостей; запускайте внутри контейнера после source install/setup.bash"
        ) from error

    rclpy.init(args=[])
    node = ChannelProbe(topic)

    def spin_until(deadline: float) -> None:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)

    try:
        print("Подключаюсь к аудио, пока НЕ говори…", flush=True)
        deadline = time.monotonic() + 5.0
        while not node.blocks and time.monotonic() < deadline:
            spin_until(min(deadline, time.monotonic() + 0.05))
        if not node.blocks:
            raise CaptureError(f"нет данных из {topic}; проверь запуск аудионоды")
        # Небольшой pre-roll; подсказка выводится только после прихода аудио.
        spin_until(time.monotonic() + 0.3)
        print(f"\n>>> {cue} <<< ({seconds:g} с после этой подсказки)", flush=True)
        spin_until(time.monotonic() + seconds)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()

    samples = np.concatenate(node.blocks)
    channel_metrics = analyse_interleaved(samples)
    metrics = channel_metrics[0]
    duration = samples.shape[0] / node.sample_rate
    print(
        f"{node.channels} каналов, {node.sample_rate} Гц, {duration:.2f} с; "
        f"разрывов: {node.gaps} (только для справки)",
        flush=True,
    )
    for channel in channel_metrics:
        print(
            f"channel {channel['channel']}: clipped {channel['clipped_samples']}; "
            f"RMS {channel['rms_dbfs']:.1f} dBFS",
            flush=True,
        )
    return samples.astype("<i2", copy=False).tobytes(), {
        "sample_rate": node.sample_rate,
        "channels": node.channels,
        "duration_seconds": duration,
        "gaps": node.gaps,
        "channel_metrics": channel_metrics,
        **metrics,
    }


def write_wav(path: Path, pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
    """Сохранить PCM16 с форматом источника без перезаписи существующего файла."""
    with path.open("xb") as stream, wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)


def _log_take(
    directory: Path,
    arguments: argparse.Namespace,
    take: Take,
    path: Path,
    metrics: dict,
    status: str,
) -> None:
    entry = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "plan": "front_distance_noise_v1",
        "kind": arguments.kind,
        "split": arguments.split,
        "speaker": getattr(arguments, "speaker", None),
        "session": arguments.session,
        "condition": take.condition,
        "take": take.number,
        "topic": arguments.topic,
        "file": str(path.relative_to(arguments.root)),
        "status": status,
        **metrics,
    }
    with (directory / "manifest.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _wait_for_enter(text: str) -> None:
    while True:
        if not input(text).strip():
            return
        print("Нажми только Enter. Для остановки используй Ctrl+C.")


def _last_entry(directory: Path, root: Path, output: Path) -> dict:
    manifest = directory / "manifest.jsonl"
    entry = {}
    if manifest.exists():
        with manifest.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                if event["file"] == str(output.relative_to(root)):
                    entry = event
    return entry


def record_plan(arguments: argparse.Namespace, directory: Path, plan: list[Take]) -> None:
    """Одним Enter запускать дубль, автоматически сохранять и переходить дальше."""
    last_condition = None
    for take in plan:
        output = directory / take.filename
        if output.exists():
            if _last_entry(directory, arguments.root, output).get("status") == "accepted":
                print(f"Уже принят, пропускаю: {take.filename}")
                continue
            # Если прошлый запуск оборвался между WAV и журналом, не принимаем
            # неизвестный дубль вслепую и не перезаписываем его — сохраняем отдельно.
            rejected = arguments.root / "rejected" / output.relative_to(arguments.root).parent
            rejected.mkdir(parents=True, exist_ok=True)
            saved = rejected / f"{output.stem}_{uuid.uuid4().hex}.wav"
            output.rename(saved)
            _log_take(directory, arguments, take, saved, {}, "rejected")
            print(f"Неподтверждённый старый дубль убран отдельно: {saved}. Запишем заново.")
        if last_condition != take.condition:
            print(f"\nУСЛОВИЯ: {take.hint}")
            last_condition = take.condition
        while True:
            _wait_for_enter(f"Дубль {take.number:02d}: нажми Enter для записи (выход — Ctrl+C): ")
            cue = (
                "ГОВОРИ: Фирая (ОДИН раз)"
                if arguments.kind == "positive"
                else "ИДЁТ ЗАПИСЬ ФОНА — НЕ ПРОИЗНОСИТЕ ФИРАЯ"
            )
            try:
                pcm, metrics = _capture(arguments.topic, arguments.seconds, cue)
            except CaptureError as error:
                print(f"Дубль не записан: {error}. Исправь причину и нажми Enter для повтора.")
                continue
            write_wav(
                output,
                pcm,
                sample_rate=metrics.get("sample_rate", 16000),
                channels=metrics.get("channels", 1),
            )
            _log_take(directory, arguments, take, output, metrics, "accepted")
            print(f"Сохранено автоматически: {output}")
            break
    print(f"\nПлан завершён: {len(plan)} WAV в {directory}")


def main(argv: list[str] | None = None) -> int:
    """Разобрать CLI и запустить сбор либо показать план без записи."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="kind", required=True)
    positive = commands.add_parser("positive", help="50 произнесений имени: 20 + 15 + 15")
    negative = commands.add_parser("negative", help="длинные фоновые фрагменты без имени")
    for command in (positive, negative):
        command.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        command.add_argument("--topic", default="/audio/mic")
        command.add_argument("--split", choices=("train", "val", "test"), default=None)
        command.add_argument("--session", help="d01 и т. п.; по умолчанию следующий свободный")
        command.add_argument("--resume", action="store_true", help="продолжить указанную сессию")
        command.add_argument("--dry-run", action="store_true", help="показать план без записи")
    positive.add_argument(
        "--speaker", required=True, help="s01–s08 train; s09–s10 val; s11–s12 test"
    )
    positive.add_argument("--seconds", type=float, default=4.0)
    negative.set_defaults(split="train")
    negative.add_argument("--condition", required=True, choices=NEGATIVE_CONDITIONS)
    negative.add_argument("--takes", type=int, default=6, help="число фрагментов; по умолчанию 6")
    negative.add_argument("--seconds", type=float, default=300.0)
    arguments = parser.parse_args(argv)
    arguments.root = arguments.root.resolve()
    try:
        directory = session_path(arguments)
        plan = build_plan(arguments)
        print(f"Сессия: {arguments.session}; выборка: {arguments.split}; дублей: {len(plan)}")
        print(f"Папка: {directory}")
        if arguments.kind == "positive":
            print(f"Человек: {arguments.speaker}. Произноси Фирая один раз после подсказки.")
        else:
            print(f"Тип фона: {arguments.condition}. Слово Фирая в записи отсутствует.")
        print("Один Enter перед каждым дублем; сохранение автоматическое; выход — Ctrl+C.")
        if arguments.dry_run:
            for take in plan:
                print(f"{take.hint}: {directory / take.filename}")
            return 0
        if not arguments.resume:
            directory.mkdir(parents=True, exist_ok=False)
        record_plan(arguments, directory, plan)
    except (KeyboardInterrupt, EOFError):
        print("\nОстановлено. Готовые WAV сохранены. Продолжение: --session НОМЕР --resume.")
        return 130
    except (ValueError, RuntimeError, OSError, wave.Error) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
