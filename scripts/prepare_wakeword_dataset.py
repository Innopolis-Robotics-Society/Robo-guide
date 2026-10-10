#!/usr/bin/env python3
"""Подготовить проверяемую черновую нарезку wake-word WAV без изменения raw.

Запуск в существующем ML/ROS-контейнере с numpy, onnxruntime и sherpa_onnx:
  python3 /home/fabian/ros2_ws/src/scripts/prepare_wakeword_dataset.py

Все результаты — кандидаты, а не подтверждённая разметка имени. Silero ищет
речь, GigaAM даёт неточные текстовые подсказки. Шумные positives и ВСЕ negative
окна требуют проверки человеком. ASR не доказывает отсутствие имени в фоне.
Никакого шумоподавления, нормализации, склеивания и изменения PCM не происходит.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .wakeword_review import write_review_page
except ImportError:  # Прямой запуск python3 scripts/prepare_wakeword_dataset.py.
    from wakeword_review import write_review_page

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RAW = REPO / "datasets/wakeword/firaya_v1/raw"
MODELS = REPO / "guide_robot_voice/models"
WORD_RE = re.compile(r"[а-яё]+", re.IGNORECASE)


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other))
            )
        previous = current
    return previous[-1]


def keyword_hints(tokens: list[str], timestamps: list[float]) -> list[dict]:
    """Найти приблизительные ASR-подсказки; совпадения НЕ являются разметкой."""
    if len(tokens) != len(timestamps):
        return []
    text = ""
    times = []
    for token, timestamp in zip(tokens, timestamps, strict=False):
        token = token.replace("▁", " ")
        text += token
        times.extend([float(timestamp)] * len(token))
    hits = []
    for match in WORD_RE.finditer(text.lower().replace("ё", "е")):
        word = match.group()
        distance = min(_distance(word, phrase) for phrase in ("фирая", "фирайя"))
        # «Феррари» наблюдалось на реальном произнесении имени; только пометка
        # для прослушивания, не автоматическое подтверждение слова «Фирая».
        if 4 <= len(word) <= 8 and (distance <= 2 or word == "феррари"):
            hits.append(
                {
                    "word": word,
                    "distance": distance,
                    "start": times[match.start()],
                    "end": times[match.end() - 1] + 0.08,
                    "exact": word in ("фирая", "фирайя"),
                }
            )
    return hits


def speech_regions(probabilities: list[float], duration: float) -> list[tuple[float, float]]:
    """Объединить VAD-окна 32 мс, сохранив короткие паузы внутри слова."""
    step = 512 / 16000
    segments = []
    start = None
    silence = None
    for index, probability in enumerate(probabilities):
        position = index * step
        if start is None:
            if probability >= 0.5:
                start = position
        elif probability >= 0.35:
            silence = None
        elif silence is None:
            silence = position
        elif position + step - silence >= 0.16:
            if silence - start >= 0.096:
                segments.append((start, silence))
            start = silence = None
    if start is not None:
        end = min(duration, silence if silence is not None else duration)
        if end - start >= 0.096:
            segments.append((start, end))
    segments = [(start, min(end, duration)) for start, end in segments if start < duration]
    merged = []
    for start, end in segments:
        if merged and start - merged[-1][1] <= 0.20:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def choose_crop(
    regions: list[tuple[float, float]],
    hints: list[dict],
    duration: float,
    noisy: bool,
    pad: float,
) -> tuple[float, float, str, list[str]]:
    """Выбрать границы, не выдавая неоднозначный шумовой фрагмент за имя."""
    reasons = []
    usable = [region for region in regions if 0.15 <= region[1] - region[0] <= 1.8]
    if not noisy and len(regions) == 1 and len(usable) == 1:
        start, end = regions[0]
        method = "single_vad_region"
        if not hints:
            reasons.append("ASR не подтвердил даже приблизительное написание имени")
        elif len(hints) > 1:
            reasons.append("ASR нашёл несколько похожих слов")
        if hints and any(hit["start"] < start - pad or hit["end"] > end + pad for hit in hints):
            # VAD может пропустить начало тихого слова. В спорном случае
            # расширяем срез, а не доверяем единственной VAD-области.
            start = min(start, *(hit["start"] for hit in hints))
            end = max(end, *(hit["end"] for hit in hints))
            method = "vad_asr_union"
            reasons.append("Границы VAD и ASR расходятся: срез расширен, проверить полное имя")
        return max(0, start - pad), min(duration, end + pad), method, reasons
    if noisy:
        reasons.append("Разговоры вокруг: проверить, что в выделение попало полное имя")
    else:
        reasons.append("Нет единственного короткого участка речи")
    if hints:
        hit = min(hints, key=lambda item: (item["distance"], item["start"]))
        start, end = hit["start"], hit["end"]
        nearby = [region for region in usable if region[0] <= end and region[1] >= start]
        if len(nearby) == 1:
            start, end = min(start, nearby[0][0]), max(end, nearby[0][1])
        if len(hints) > 1:
            reasons.append("Несколько ASR-подсказок; выбран только один черновой кандидат")
        padding = max(pad, 0.35)
        return max(0, start - padding), min(duration, end + padding), "asr_hint", reasons
    if len(usable) == 1 and len(regions) == 1:
        start, end = usable[0]
        reasons.append("Выделена речь, но наличие имени не подтверждено")
        return max(0, start - pad), min(duration, end + pad), "vad_only", reasons
    reasons.append("Надёжных границ имени нет: сохранён весь исходник без обрезки")
    return 0.0, duration, "full_recording", reasons


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Прочитать PCM16 mono @ 16 кГц без преобразования сигнала."""
    with wave.open(str(path), "rb") as recording:
        if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise ValueError(f"ожидается mono PCM16 16000 Гц: {path}")
        pcm = recording.readframes(recording.getnframes())
    if not pcm:
        raise ValueError(f"пустой WAV: {path}")
    return np.frombuffer(pcm, dtype="<i2"), 16000


def write_crop(path: Path, samples: np.ndarray, start: float, end: float) -> tuple[int, int]:
    """Сохранить срез исходных отсчётов, без обработки и без перезаписи."""
    first = max(0, int(np.floor(start * 16000)))
    last = min(len(samples), int(np.ceil(end * 16000)))
    if first >= last:
        raise ValueError("пустой интервал обрезки")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream, wave.open(stream, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(samples[first:last].astype("<i2", copy=False).tobytes())
    return first, last


def quality_metrics(samples: np.ndarray) -> dict:
    """Вычислить метрики только для отчёта, ничего не отбрасывая."""
    audio = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(audio * audio)))
    peak = float(np.max(np.abs(audio)))
    return {
        "rms_dbfs": float(20 * np.log10(rms / 32768)) if rms else -120.0,
        "peak_dbfs": float(20 * np.log10(peak / 32768)) if peak else -120.0,
        "clipped_samples": int(np.count_nonzero(np.abs(audio) >= 32767)),
    }


class Analyser:
    """Переиспользовать локальные CPU-модели для всех файлов."""

    def __init__(self) -> None:
        """Загрузить существующие модели без сети и без изменения ROS-настроек."""
        import sherpa_onnx

        sys.path.insert(0, str(REPO / "guide_robot_voice"))
        from guide_robot_voice.lib.vad_model import SileroVad

        self.vad = SileroVad(str(MODELS / "silero_vad.onnx"))
        self.vad.load()
        self.asr = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            tokens=str(MODELS / "gigaam_v3_ctc_tokens.txt"),
            model=str(MODELS / "gigaam_v3_ctc_int8.onnx"),
            num_threads=2,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )

    def decode(self, samples: np.ndarray) -> dict:
        """Получить текст и временные подсказки; ASR не является истиной."""
        stream = self.asr.create_stream()
        stream.accept_waveform(16000, samples.astype(np.float32) / 32768)
        self.asr.decode_stream(stream)
        result = stream.result
        return {
            "text": result.text,
            "hints": keyword_hints(list(result.tokens), list(result.timestamps)),
            "tokens": list(result.tokens),
            "timestamps": list(result.timestamps),
        }

    def speech(self, samples: np.ndarray) -> list[tuple[float, float]]:
        """Найти речь с независимым состоянием VAD для каждого WAV."""
        self.vad.reset()
        probabilities = []
        # Дополнительная тишина позволяет VAD завершить последний участок.
        padded = np.pad(samples, (0, 4096 + (-len(samples) % 512)))
        for start in range(0, len(padded), 512):
            probabilities.append(self.vad.process(padded[start : start + 512]))
        return speech_regions(probabilities, len(samples) / 16000)


def _source_metadata(path: Path) -> dict:
    manifest = path.parent / "manifest.jsonl"
    entry = {}
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if Path(item.get("file", "")).name == path.name:
                entry = item
    return entry


def _row(
    path: Path,
    raw: Path,
    samples: np.ndarray,
    candidate: Path,
    output: Path,
    start: int,
    end: int,
    kind: str,
    source_hash: str | None = None,
) -> dict:
    meta = _source_metadata(path)
    return {
        "source": str(path.relative_to(raw)),
        "source_sha256": source_hash or hashlib.sha256(path.read_bytes()).hexdigest(),
        "candidate": str(candidate.relative_to(output)),
        "kind": kind,
        "split": path.relative_to(raw).parts[1],
        "crop_start_sample": start,
        "crop_end_sample": end,
        "crop_start_seconds": start / 16000,
        "crop_end_seconds": end / 16000,
        "source_seconds": len(samples) / 16000,
        "candidate_seconds": (end - start) / 16000,
        "source_gaps": meta.get("gaps"),
        "label_verified": False,
        **quality_metrics(samples[start:end]),
    }


def process_positive(path: Path, raw: Path, output: Path, analyser: Analyser, pad: float) -> dict:
    """Подготовить один positive-кандидат с неизменным фоном внутри среза."""
    samples, _ = read_wav(path)
    regions = analyser.speech(samples)
    recognition = analyser.decode(samples)
    noisy = path.name.startswith("crowd_")
    hints = recognition["hints"]
    # Короткое окно иногда позволяет ASR услышать имя, которое теряется
    # при декодировании всего разговора. Любой такой результат требует проверки.
    used_short_windows = False
    if noisy and not hints:
        for start_seconds in (0.3, 0.7, 1.1, 1.5):
            first = int(start_seconds * 16000)
            part = samples[first : first + int(1.8 * 16000)]
            decoded = analyser.decode(part)
            for hit in decoded["hints"]:
                hints.append(
                    {
                        **hit,
                        "start": hit["start"] + start_seconds,
                        "end": hit["end"] + start_seconds,
                    }
                )
            if decoded["hints"]:
                used_short_windows = True
                break
    start, end, method, reasons = choose_crop(regions, hints, len(samples) / 16000, noisy, pad)
    if used_short_windows:
        reasons.append("ASR-подсказка найдена только в коротком поисковом окне")
    candidate = output / "positive_candidates" / path.relative_to(raw / "positive")
    first, last = write_crop(candidate, samples, start, end)
    row = _row(path, raw, samples, candidate, output, first, last, "positive")
    if row["clipped_samples"]:
        reasons.append("Есть предельные отсчёты; клиппинг не фильтровался")
    if row["source_gaps"]:
        reasons.append("Журнал исходника содержит разрывы потока")
    if row["candidate_seconds"] < 0.6:
        reasons.append("Короткий фрагмент: проверить, что имя не обрезано")
    if last == len(samples) and method != "full_recording":
        reasons.append("Выделение доходит до конца исходника: проверить окончание имени")
    row.update(
        {
            "method": method,
            "asr_text": recognition["text"],
            "asr_hints": hints,
            "vad_regions": regions,
            "review_required": bool(reasons) or noisy,
            "status": "needs_review" if reasons or noisy else "auto_boundary_candidate",
            "review_reasons": reasons,
        }
    )
    return row


def process_negative(
    path: Path,
    raw: Path,
    output: Path,
    analyser: Analyser,
    window: float,
) -> tuple[list[dict], dict]:
    """Нарезать фон, все окна оставить неподтверждёнными; отметить похожие имена."""
    samples, _ = read_wav(path)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    hints = []
    transcript = []
    for start in range(0, len(samples), 8 * 16000):
        if len(samples) - start < 1600:
            break
        decoded = analyser.decode(samples[start : start + 10 * 16000])
        offset = start / 16000
        transcript.append({"start": offset, "text": decoded["text"]})
        for hit in decoded["hints"]:
            hit = {**hit, "start": hit["start"] + offset, "end": hit["end"] + offset}
            if not any(abs(hit["start"] - old["start"]) < 0.5 for old in hints):
                hints.append(hit)
    rows = []
    size = int(window * 16000)
    parent = output / "negative_pending" / path.relative_to(raw / "negative").parent
    for number, first in enumerate(range(0, len(samples), size), 1):
        last = min(first + size, len(samples))
        if last - first < 1600:
            continue
        candidate = parent / f"{path.stem}_part{number:04d}.wav"
        first, last = write_crop(candidate, samples, first / 16000, last / 16000)
        row = _row(path, raw, samples, candidate, output, first, last, "negative", source_hash)
        nearby = [
            hit
            for hit in hints
            if hit["start"] - 0.5 < last / 16000 and hit["end"] + 0.5 > first / 16000
        ]
        row.update(
            {
                "method": "fixed_windows",
                "asr_text": "",
                "asr_hints": nearby,
                "vad_regions": [],
                "review_required": True,
                "status": "possible_keyword" if nearby else "negative_unverified",
                "review_reasons": [
                    "ASR не доказывает отсутствие имени: прослушать фон перед отрицательной меткой"
                ]
                + (
                    ["Найдено похожее на имя слово: НЕ использовать как negative"]
                    if nearby
                    else []
                ),
            }
        )
        rows.append(row)
    return rows, {"source": str(path.relative_to(raw)), "hints": hints, "transcript": transcript}


def write_reports(output: Path, raw: Path, rows: list[dict], scans: list[dict]) -> dict:
    """Сохранить машинные отчёты и локальную страницу для прослушивания кандидатов."""
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_root": str(raw),
        "output_root": str(output),
        "source_positive_files": sum(row["kind"] == "positive" for row in rows),
        "source_negative_files": len(scans),
        "negative_windows": sum(row["kind"] == "negative" for row in rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "training_ready": False,
        "warning": "Черновая нарезка; noisy positives и все negative требуют проверки. "
        "label_verified=false для всех файлов. Исходный PCM внутри срезов сохранён.",
    }
    for filename, value in (("summary.json", summary), ("negative_scan.json", scans)):
        (output / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with (output / "manifest.jsonl").open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    columns = [
        "source",
        "candidate",
        "status",
        "review_required",
        "crop_start_seconds",
        "crop_end_seconds",
        "candidate_seconds",
        "method",
        "asr_text",
        "clipped_samples",
        "source_gaps",
        "review_reasons",
    ]
    with (output / "review.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "review_reasons": "; ".join(row["review_reasons"])})
    write_review_page(output, raw, rows, scans)
    return summary


def main(argv: list[str] | None = None) -> int:
    """Обработать снимок текущих WAV в новую папку, не перезаписывая результаты."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output", type=Path, help="новая папка; по умолчанию prepared/run_NN")
    parser.add_argument("--pad", type=float, default=0.2)
    parser.add_argument("--negative-window", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not np.isfinite(args.pad) or not 0 <= args.pad <= 1:
        parser.error("--pad должен быть от 0 до 1 секунды")
    if not np.isfinite(args.negative_window) or args.negative_window < 0.1:
        parser.error("--negative-window должен быть не меньше 0.1 секунды")
    raw = args.raw_root.resolve()
    positives = sorted((raw / "positive").rglob("*.wav"))
    negatives = sorted((raw / "negative").rglob("*.wav"))
    if not positives and not negatives:
        parser.error("нет WAV для обработки")
    if args.output:
        output = args.output.resolve()
    else:
        parent = raw.parent / "prepared"
        number = 1
        while (parent / f"run_{number:02d}").exists():
            number += 1
        output = parent / f"run_{number:02d}"
    if output == raw or raw in output.parents:
        parser.error("папка результатов не должна находиться внутри raw")
    output.mkdir(parents=True, exist_ok=False)
    print(
        f"Positive: {len(positives)}; negative: {len(negatives)}; " f"результат: {output}",
        flush=True,
    )
    initial_hashes = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in positives + negatives
    }
    analyser = Analyser()
    rows = []
    scans = []
    for index, path in enumerate(positives, 1):
        row = process_positive(path, raw, output, analyser, args.pad)
        rows.append(row)
        print(
            f"positive {index}/{len(positives)}: {path.name} → {row['method']}, "
            f"{row['candidate_seconds']:.2f} с ({row['status']})",
            flush=True,
        )
    for index, path in enumerate(negatives, 1):
        print(f"negative {index}/{len(negatives)}: {path.name}; поиск подсказок…", flush=True)
        parts, scan = process_negative(path, raw, output, analyser, args.negative_window)
        rows.extend(parts)
        scans.append(scan)
        print(f"negative: {len(parts)} окон; ASR-подсказок: {len(scan['hints'])}", flush=True)
    changed = [
        str(path)
        for path in positives + negatives
        if hashlib.sha256(path.read_bytes()).hexdigest() != initial_hashes[str(path)]
    ]
    if changed:
        raise RuntimeError(f"исходники изменились во время обработки: {changed}")
    summary = write_reports(output, raw, rows, scans)
    summary["source_hashes_unchanged"] = True
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
