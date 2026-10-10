#!/usr/bin/env python3
"""Локальный Silero MIT-пилот: сначала проверка произношения, затем датасет.

По умолчанию только показать план: не импортировать torch, не обращаться к сети.
--execute создаёт WAV, --download-model явно разрешает загрузку официального веса.
Нужны torch, numpy, scipy (уже установлены в amd-terminal). API-ключи не нужны.
Оригинальные записи, текущая KWS-модель и ROS-конфигурация не меняются.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "datasets/wakeword/firaya_v1/synthetic"
MODEL = SYNTHETIC / "_silero/v5_cis_base.pt"
MODEL_URL = "https://models.silero.ai/models/tts/ru/v5_cis_base.pt"
LICENSE_URL = "https://github.com/snakers4/silero-models/blob/master/LICENSE_CIS"
SPEAKERS = ("ru_eduard", "ru_albina", "ru_dmitriy", "ru_ekaterina")
STRESSES = {"a": "Фир+ая", "i": "Ф+ирая", "ya": "Фира+я"}
POSITIVE = (
    "{name}.",
    "{name}!",
    "{name}, расскаж+и про карт+ину.",
    "Скаж+и, {name}, где в+ыход?",
    "Прив+ет, {name}.",
    "Мы провер+яем микроф+он. {name}, ты зд+есь?",
)
NEGATIVE = (
    "Ферр+ари.",
    "Ф+иллер.",
    "Фонв+изин.",
    "Ф+идер.",
    "Ф+ак.",
    "Ф+ирма.",
    "Фиг+ура.",
    "Фи+алка.",
    "Фин+ал.",
    "Ф+ильтр.",
    "Февр+аль.",
    "Я в+идел кр+асную ферр+ари.",
    "Н+ужно замен+ить ф+ильтр.",
    "Пров+ерим ф+идер и мод+ель.",
    "Сег+одня мн+ого люд+ей, и все разгов+аривают.",
    "Расскаж+и про +эту карт+ину и покаж+и дор+огу к в+ыходу.",
)
# Contrastive words, partial names, other names and ordinary robot-directed speech.
# The boolean holds out the complete word AND its contextual phrase (all voices,
# tempos and subsequent augmentations), not just another rendition of train text.
HARD_WORDS = (
    ("Фар+ида", False),
    ("Фи+она", False),
    ("Фл+ора", False),
    ("Ф+ёдор", False),
    ("Ф+едя", True),
    ("Фом+а", False),
    ("Фил+ипп", False),
    ("Ф+ея", False),
    ("Ф+ира", False),
    ("Ф+ая", False),
    ("Р+ая", False),
    ("+Ария", False),
    ("Мар+ия", False),
    ("Д+арья", True),
    ("Ф+урия", False),
    ("Ф+ура", False),
    ("Фур+ажка", False),
    ("Ф+ирменный", False),
    ("Ф+ирмы", False),
    ("Фили+ал", False),
    ("Филарм+ония", False),
    ("Фи+аско", False),
    ("Ф+изика", False),
    ("Филос+офия", True),
    ("Физиол+огия", False),
    ("Ф+икус", False),
    ("Ф+иниш", False),
    ("Фин+ансы", False),
    ("Фигн+я", False),
    ("Фе+ерия", True),
    ("Фр+аза", False),
    ("Фр+акция", False),
    ("Ф+риз", False),
    ("Эф+ир", False),
    ("Эф+ира", False),
    ("Эф+ирная", False),
    ("Те+ория", False),
    ("Галер+ея", True),
    ("Батар+ея", False),
    ("Ист+ория", False),
    ("Пир+ог", False),
    ("Пир+аты", False),
    ("Пирам+ида", True),
    ("Ряб+ина", False),
    ("Мод+ель", False),
    ("Р+обот", False),
    ("Карт+ина", False),
    ("Ком+анда", False),
)
HARD_PHRASES = (
    "Расскаж+и про карт+ину.",
    "Покаж+и, где в+ыход.",
    "Прив+ет, как дел+а?",
    "Ты мен+я сл+ышишь?",
    "Вкл+ючи микроф+он.",
    "Эф+ир уже нач+ался.",
    "У ф+ирмы н+овый фили+ал.",
    "Мы провер+яем мод+ель и р+обота.",
    "Хор+ошо, спас+ибо.",
    "Подожд+и немн+ого.",
    "Расскаж+и, что здесь происх+одит.",
    "Назов+и им+я худ+ожника.",
)
MIT_LICENSE = """MIT License

Copyright (c) 2025-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def encode_json(value: object) -> bytes:
    """Сериализовать планы и результаты стабильно для проверки resume."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write_new(path: Path, data: bytes) -> None:
    """Создать файл атомарно, без перезаписи; удалить только свой временный файл."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".silero-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def download_model(path: Path) -> None:
    """Загрузить только официальный v5_cis_base, без hub и сторонних зеркал."""
    if path.exists():
        print(f"Вес уже существует, не перезаписываем: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Загрузка MIT-модели: {MODEL_URL}", flush=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".download-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=15) as response:
                total = 0
                digest = hashlib.sha256()
                while block := response.read(1024 * 1024):
                    stream.write(block)
                    digest.update(block)
                    total += len(block)
                    if total > 300 * 1024 * 1024:
                        raise ValueError("размер модели превысил 300 MiB; загрузка прервана")
                if total < 10 * 1024 * 1024:
                    raise ValueError("слишком маленький ответ сервера вместо модели")
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink()
    receipt = path.with_suffix(".download.json")
    write_new(
        receipt,
        encode_json(
            {
                "url": MODEL_URL,
                "sha256": digest.hexdigest(),
                "bytes": total,
                "license": "MIT",
                "license_url": LICENSE_URL,
            }
        ),
    )
    license_path = path.parent / "LICENSE_CIS"
    if not license_path.exists():
        write_new(license_path, MIT_LICENSE.encode())


def load_model(path: Path, threads: int):
    """Загрузить доверенный локальный torch.package на CPU, проверяя SHA кэша."""
    import torch

    if not path.is_file():
        raise FileNotFoundError(
            f"нет {path}. Добавьте --download-model или скачайте официальный вес в этот файл"
        )
    receipt = path.with_suffix(".download.json")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if receipt.exists() and json.loads(receipt.read_text())["sha256"] != digest:
        raise ValueError("SHA модели не совпадает с квитанцией загрузки; запуск остановлен")
    if threads < 1:
        raise ValueError("--threads должен быть положительным")
    torch.set_num_threads(threads)
    model = torch.package.PackageImporter(str(path)).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    return model, digest


def make_plan(mode: str, speakers: list[str], stress: str | None) -> dict:
    """Проверка ударений отдельно от train-only пилота; обе метки имеют те же голоса."""
    if mode not in ("pronunciation", "pilot", "hard_negatives"):
        raise ValueError("неизвестный режим")
    if (
        not speakers
        or len(speakers) != len(set(speakers))
        or any(not re.fullmatch(r"ru_[a-z0-9_]+", speaker) for speaker in speakers)
    ):
        raise ValueError("нужны уникальные русские speaker ID ru_*")
    if mode != "pronunciation" and stress not in STRESSES:
        raise ValueError("для pilot укажите --wakeword-stress a, i или ya после прослушивания")
    texts = []
    if mode == "pronunciation":
        for accent, name in STRESSES.items():
            texts.extend(
                ("positive", template.format(name=name), accent, label)
                for template, label in (("{name}.", "word"), ("Прив+ет, {name}.", "phrase"))
            )
        texts.extend(
            ("negative", text, None, f"negative_{index:02d}")
            for index, text in enumerate(NEGATIVE[:5], 1)
        )
        rates = [100]
    else:
        texts.extend(
            ("positive", template.format(name=STRESSES[stress]), stress, f"positive_{index:02d}")
            for index, template in enumerate(POSITIVE, 1)
        )
        texts.extend(
            ("negative", text, None, f"negative_{index:02d}")
            for index, text in enumerate(NEGATIVE, 1)
        )
        rates = [85, 100, 115]
        if mode == "hard_negatives":
            for index, (word, _) in enumerate(HARD_WORDS, 1):
                texts.extend(
                    ("negative", text, None, f"hard_{index:02d}_{context}")
                    for context, text in (
                        ("word", f"{word}."),
                        ("phrase", f"Я произнош+у сл+ово {word}."),
                    )
                )
            texts.extend(
                ("negative", text, None, f"ordinary_{index:02d}")
                for index, text in enumerate(HARD_PHRASES, 1)
            )
    samples = []
    for speaker in speakers:
        for rate in rates:
            for kind, text, accent, label in texts:
                params = {
                    "speaker": speaker,
                    "text_with_stress": text,
                    "rate_percent": rate,
                    "tts_sample_rate": 24000,
                    "seed": 1337,
                    "model_id": "v5_cis_base",
                }
                identity = hashlib.sha256(encode_json(params)).hexdigest()
                name = f"{accent}_{label}" if accent else label
                sample = {
                    "id": identity,
                    "kind": kind,
                    "text": text.replace("+", ""),
                    "parameters": params,
                    "stress": accent,
                    "file": f"{kind}/{speaker}/{name}_{rate}.wav",
                }
                if mode == "hard_negatives":
                    held_out = label in {
                        "negative_06",
                        "negative_09",
                        "negative_12",
                        "negative_16",
                    }
                    if label.startswith("hard_"):
                        held_out = HARD_WORDS[int(label.split("_")[1]) - 1][1]
                    elif label.startswith("ordinary_"):
                        held_out = label in {"ordinary_09", "ordinary_12"}
                    sample["training_split"] = "val" if held_out else "train"
                samples.append(sample)
    return {
        "schema_version": 1,
        "provider": "silero",
        "model_id": "v5_cis_base",
        "license": "MIT",
        "license_url": LICENSE_URL,
        "mode": mode,
        "sample_rate": 16000,
        "sample_width": 2,
        "channels": 1,
        "split": "review_only" if mode == "pronunciation" else "train_only",
        "review_required": True,
        "training_ready": False,
        "samples": samples,
    }


def synthesize(model, parameters: dict) -> bytes:
    """SSML-темп + явные ударения; resample 24k→16k с антиалиасингом, без нормализации."""
    import numpy as np
    import torch
    from scipy.signal import resample_poly

    text = html.escape(parameters["text_with_stress"])
    rate = parameters["rate_percent"]
    ssml = f'<speak><prosody rate="{rate}%">{text}</prosody></speak>'
    torch.manual_seed(parameters["seed"])
    with torch.inference_mode():
        audio = model.apply_tts(
            ssml_text=ssml,
            speaker=parameters["speaker"],
            sample_rate=parameters["tts_sample_rate"],
        )
    audio = np.asarray(audio.detach().cpu().numpy(), dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(audio)) or not 2400 <= audio.size <= 24000 * 60:
        raise ValueError("неожиданный синтез: длина или NaN/Inf")
    audio = resample_poly(audio, 2, 3)
    if float(np.max(np.abs(audio))) < 1e-5:
        raise ValueError("синтезатор вернул тишину")
    pcm = np.clip(np.rint(audio * 32767), -32768, 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


def review_html(samples: list[dict]) -> bytes:
    """Показать текст, ударение и встроенный проигрыватель каждого результата."""
    parts = [
        '<!doctype html><meta charset="utf-8"><title>Silero: проверка произношения</title>',
        "<style>body{font:18px sans-serif;max-width:1000px;margin:30px auto}"
        "article{padding:12px;border-bottom:1px solid #ccc}"
        "code{white-space:pre-wrap}</style>",
        "<h1>Silero: проверка произношения</h1>",
        "<p>«+» перед гласной обозначает ударение. Проверить имя целиком, ударение, "
        "естественность голоса и отсутствие имени в отрицательных примерах. "
        "Эти файлы ещё не приняты в обучение.</p>",
    ]
    for row in samples:
        name = html.escape(row["file"], quote=True)
        text = html.escape(row["parameters"]["text_with_stress"])
        parts.append(
            f'<article><div>{html.escape(row["kind"])} — <code>{name}</code></div>'
            f'<p>{text}</p><audio controls preload="none" src="{name}"></audio></article>'
        )
    return "\n".join(parts).encode()


def generate(model, digest: str, plan: dict, output: Path, resume: bool) -> int:
    """Создать отдельный набор, сохраняя план, хэши и незавершённые файлы."""
    unknown = {row["parameters"]["speaker"] for row in plan["samples"]} - set(model.speakers)
    if unknown:
        raise ValueError(f"неизвестные голоса: {sorted(unknown)}; используйте --list-speakers")
    if output.exists() and not resume:
        raise ValueError("папка уже существует; добавьте --resume или используйте другую папку")
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".generation.lock"
    write_new(lock, b"single writer\n")
    try:
        saved = {**plan, "model_sha256": digest}
        path = output / "plan.json"
        if path.exists():
            if json.loads(path.read_text()) != saved:
                raise ValueError("план или вес изменён; существующий набор не перезаписываем")
        else:
            write_new(path, encode_json(saved))
        license_path = output / "LICENSE_CIS"
        if not license_path.exists():
            write_new(license_path, MIT_LICENSE.encode())
        metadata = output / "metadata"
        metadata.mkdir(exist_ok=True)
        rows = []
        created = 0
        for index, sample in enumerate(plan["samples"], 1):
            target = output / sample["file"]
            record_path = metadata / f"{sample['id']}.json"
            if record_path.exists():
                row = json.loads(record_path.read_text())
                if (
                    row["sample"] != sample
                    or not target.is_file()
                    or (hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"])
                ):
                    raise ValueError(f"повреждён файл {target}; не перезаписываем")
            else:
                if target.exists():
                    raise ValueError(f"файл без metadata: {target}; сохраните его для разбора")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = synthesize(model, sample["parameters"])
                write_new(target, data)
                with wave.open(io.BytesIO(data)) as wav:
                    seconds = wav.getnframes() / wav.getframerate()
                row = {
                    "sample": sample,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "seconds": seconds,
                    "provider": "silero",
                    "model_sha256": digest,
                    "license": "MIT",
                    "split": plan["split"],
                    "review_required": True,
                    "label_verified": False,
                }
                write_new(record_path, encode_json(row))
                created += 1
                print(f"{index}/{len(plan['samples'])} {sample['file']}", flush=True)
            rows.append(row)
        reports = {
            "manifest.jsonl": b"".join(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode() + b"\n"
                for row in rows
            ),
            "review.html": review_html(plan["samples"]),
        }
        for filename, data in reports.items():
            target = output / filename
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError(f"изменён {target}; не перезаписываем")
            else:
                write_new(target, data)
        return created
    finally:
        lock.unlink()


def main(argv: list[str] | None = None) -> int:
    """Сначала дать офлайн-план; создание аудио и загрузка только по явным флагам."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("pronunciation", "pilot", "hard_negatives"), default="pronunciation"
    )
    parser.add_argument("--speaker", action="append", help="ru_* ID; можно повторять")
    parser.add_argument(
        "--wakeword-stress",
        choices=tuple(STRESSES),
        help="a=ФирАя, i=ФИрая, ya=ФираЯ; обязателен для pilot",
    )
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--download-model", action="store_true")
    parser.add_argument("--list-speakers", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args.mode, args.speaker or list(SPEAKERS), args.wakeword_stress)
        output = args.output or SYNTHETIC / f"silero_{args.mode}_01"
        if args.threads < 1:
            raise ValueError("--threads должен быть положительным")
        counts = dict(Counter(sample["kind"] for sample in plan["samples"]))
        print(f"План: {len(plan['samples'])} WAV; {counts}")
        print(f"Папка: {output}\nSilero v5_cis_base, MIT; локально, без API-ключа.")
        if not args.execute and not args.list_speakers:
            print("Предпросмотр: ничего не скачано и не сгенерировано.")
            return 0
        if args.download_model:
            download_model(args.model)
        model, digest = load_model(args.model, args.threads)
        if args.list_speakers:
            print("\n".join(sorted(name for name in model.speakers if name.startswith("ru_"))))
            return 0
        count = generate(model, digest, plan, output, args.resume)
        print(f"Готово: новых WAV {count}. Откройте {output / 'review.html'}.")
        print("Обучение не запускалось. Сначала подтвердите произношение.")
        return 0
    except (
        ValueError,
        OSError,
        RuntimeError,
        ImportError,
        KeyError,
        urllib.error.URLError,
    ) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
