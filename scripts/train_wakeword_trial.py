#!/usr/bin/env python3
"""CPU-прототип openWakeWord на реальных XVF-записях, без ROS-изменений.

Изолированные зависимости: scripts/requirements_wakeword_trial.txt.
Frozen upstream feature extractor + небольшой обучаемый PyTorch-классификатор.
Положительные записи имеют метку на уровне файла, поэтому используется
multiple-instance loss: только максимум среди окон записи является positive.
Не размечаем каждый голос/каждое окно четырёхсекундной записи как имя.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html
import importlib.metadata
import json
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "datasets/wakeword/firaya_v1"
FRAME = 1280
RATE = 16000
CONTEXT = 16
PREFIX = 2.0
TAIL = 1.28


def read_wav(path: Path) -> np.ndarray:
    """Прочитать WAV без конвертации и нормализации."""
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise ValueError(f"ожидается mono PCM16 @16k: {path}")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()


def resolve_feature_cache(run: Path) -> Path:
    """Найти настоящий кэш, если предыдущий опыт сам переиспользовал признаки."""
    visited = set()
    while True:
        run = run.resolve()
        if run in visited:
            raise ValueError("циклическая ссылка на кэш признаков")
        visited.add(run)
        provenance = run / "feature_provenance.json"
        source = (
            json.loads(provenance.read_text()).get("reused_from") if provenance.exists() else None
        )
        # A mixed run may have only newly computed synthetic features locally.
        # Its real feature cache still belongs to the original source run.
        if source:
            run = Path(source)
            continue
        if any((run / "features").glob("*.npy")):
            return run
        if not provenance.exists():
            raise ValueError(f"нет кэша признаков: {run}")
        raise ValueError(f"не указано расположение кэша: {run}")


def plan_split(rows: list[dict], val_speaker: str, test_speaker: str) -> dict:
    """Отложить целых людей и целые фоновые исходники до обучения."""
    if val_speaker == test_speaker:
        raise ValueError("validation и test должны содержать разных людей")
    positive = [row for row in rows if row["kind"] == "positive"]
    speakers = {Path(row["source"]).parts[2] for row in positive}
    if not {val_speaker, test_speaker} <= speakers or len(speakers) < 3:
        raise ValueError("не хватает независимых людей для train/val/test")
    result = {
        split: {"positive": [], "negative_sources": []} for split in ("train", "val", "test")
    }
    for row in positive:
        speaker = Path(row["source"]).parts[2]
        split = "test" if speaker == test_speaker else "val" if speaker == val_speaker else "train"
        result[split]["positive"].append(row)
    negative = sorted({row["source"] for row in rows if row["kind"] == "negative"})
    if len(negative) < 3:
        raise ValueError("нужны хотя бы 3 отдельных фоновых исходника")
    result["train"]["negative_sources"] = negative[:-2]
    result["val"]["negative_sources"] = negative[-2:-1]
    result["test"]["negative_sources"] = negative[-1:]
    return result


def max_confirmed_score(scores: np.ndarray, consecutive_frames: int = 2) -> float:
    """Максимум, подтверждённый двумя последовательными 80-мс кадрами."""
    if consecutive_frames < 1:
        raise ValueError("consecutive_frames must be positive")
    if len(scores) < consecutive_frames:
        return 0.0
    return float(
        np.lib.stride_tricks.sliding_window_view(scores, consecutive_frames).min(axis=1).max()
    )


def event_indices(
    scores: np.ndarray, threshold: float, cooldown: float = 2.0, consecutive_frames: int = 2
) -> list[int]:
    """Считать активации с двумя кадрами подтверждения и фиксированным cooldown."""
    events = []
    if consecutive_frames < 1:
        raise ValueError("consecutive_frames must be positive")
    streak = 0
    last = -1e9
    for index, value in enumerate(scores):
        streak = streak + 1 if value >= threshold else 0
        if streak >= consecutive_frames and (index - last) * FRAME / RATE >= cooldown:
            events.append(index)
            last = index
    return events


def choose_threshold(
    positive_scores: list[np.ndarray],
    negative_scores: list[np.ndarray],
    margin: float = 0.005,
    consecutive_frames: int = 2,
) -> dict:
    """Настроить порог только по validation: сначала минимизировать ложные активации."""
    largest_negative = max(
        (max_confirmed_score(score, consecutive_frames) for score in negative_scores), default=0
    )
    # Дополнительный порог чуть выше худшего negative — не использует test.
    safe = min(1.0, largest_negative + margin)
    thresholds = sorted(set([*np.linspace(0.05, 0.95, 19), 0.97, 0.99, 0.995, safe]))
    trials = []
    for threshold in thresholds:
        threshold = float(threshold)
        hits = sum(
            max_confirmed_score(score, consecutive_frames) >= threshold
            for score in positive_scores
        )
        false = sum(
            len(event_indices(score, threshold, consecutive_frames=consecutive_frames))
            for score in negative_scores
        )
        trials.append(
            {
                "threshold": threshold,
                "hits": hits,
                "total": len(positive_scores),
                "recall": hits / len(positive_scores),
                "false_events": false,
            }
        )
    return min(
        trials,
        key=lambda row: (
            row["false_events"] > 0,
            row["false_events"],
            -row["recall"],
            row["threshold"],
        ),
    )


def stream_features(extractor, audio: np.ndarray, skip_seconds: float) -> np.ndarray:
    """Кэшировать те же streaming features, которые использует Model.predict."""
    extractor.reset()
    frames = []
    for first in range(0, len(audio), FRAME):
        chunk = audio[first : first + FRAME]
        if len(chunk) < FRAME:
            chunk = np.pad(chunk, (0, FRAME - len(chunk)))
        extractor(chunk)
        if (first + FRAME) / RATE > skip_seconds:
            frames.append(extractor.get_features(CONTEXT)[0].copy())
    return np.asarray(frames, dtype=np.float32)


def prepare_features(
    plan: dict,
    raw: Path,
    output: Path,
    backbone: Path,
    seed: int,
    reuse: Path | None = None,
) -> dict:
    """Вычислить frozen features; контекст каждого split берётся только из его фона."""
    from openwakeword.utils import AudioFeatures

    np.random.seed(seed)
    extractor = (
        None
        if reuse
        else AudioFeatures(
            melspec_model_path=str(backbone / "melspectrogram.onnx"),
            embedding_model_path=str(backbone / "embedding_model.onnx"),
            inference_framework="onnx",
            ncpu=1,
        )
    )
    features = {}
    cache = output / "features"
    cache.mkdir()
    for split, data in plan.items():
        negatives = []
        negative_audio = []
        for source in data["negative_sources"]:
            audio = read_wav(raw / source)
            negative_audio.append(audio)
            cached = reuse / "features" / f"{split}_{Path(source).stem}.npy" if reuse else None
            value = (
                np.load(cached, allow_pickle=False)
                if cached
                else stream_features(extractor, audio, PREFIX)
            )
            negatives.append(
                {
                    "source": source,
                    "features": value,
                    "seconds": len(audio) / RATE,
                    "evaluated_seconds": len(audio) / RATE - PREFIX,
                }
            )
            if not reuse:
                np.save(cache / f"{split}_{Path(source).stem}.npy", value)
            print(f"{split}: фон {source}: {len(value)} кадров", flush=True)
        context = np.concatenate(negative_audio)
        positives = []
        for index, row in enumerate(data["positive"]):
            # Проверяется полноценный оригинал, а не тишина вокруг короткой нарезки.
            # Имя известно только на уровне файла. Все rejected отсутствуют в plan.
            audio = read_wav(raw / row["source"])
            offset = (index * 11713) % (len(context) - int((PREFIX + TAIL) * RATE))
            before = context[offset : offset + int(PREFIX * RATE)]
            after = context[offset + int(PREFIX * RATE) : offset + int((PREFIX + TAIL) * RATE)]
            stream = np.concatenate([before, audio, after])
            cached = reuse / "features" / f"{split}_positive_{index:03d}.npy" if reuse else None
            value = (
                np.load(cached, allow_pickle=False)
                if cached
                else stream_features(extractor, stream, PREFIX)
            )
            positives.append(
                {
                    "row": row,
                    "features": value,
                    "source_seconds": len(audio) / RATE,
                    "context_offset": offset,
                }
            )
            if not reuse:
                np.save(cache / f"{split}_positive_{index:03d}.npy", value)
            if (index + 1) % 10 == 0 or index + 1 == len(data["positive"]):
                print(f"{split}: positive {index + 1}/{len(data['positive'])}", flush=True)
        features[split] = {"positive": positives, "negative": negatives}
    return features


def build_model(
    mean: np.ndarray,
    std: np.ndarray,
    width: int = 32,
    layer_norm: bool = True,
    architecture: str = "dnn",
):
    """Создать маленькую классификационную голову; backbone не обучается."""
    import torch
    from torch import nn

    if architecture == "tcn":

        class TemporalHead(nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer("mean", torch.from_numpy(mean))
                self.register_buffer("std", torch.from_numpy(std))
                self.net = nn.Sequential(
                    nn.Conv1d(96, width, 3, padding=1),
                    nn.GroupNorm(8, width),
                    nn.ReLU(),
                    nn.Dropout(0.1),
                    nn.Conv1d(width, width, 3, padding=2, dilation=2),
                    nn.GroupNorm(8, width),
                    nn.ReLU(),
                    nn.AdaptiveMaxPool1d(1),
                    nn.Flatten(),
                    nn.Linear(width, 1),
                )

            def forward(self, value):
                return self.net(((value - self.mean) / self.std).transpose(1, 2))

        if width % 8:
            raise ValueError("TCN width must be divisible by 8")
        return TemporalHead()
    if architecture != "dnn":
        raise ValueError("unknown architecture")

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.register_buffer("mean", torch.from_numpy(mean))
            self.register_buffer("std", torch.from_numpy(std))
            layers = [nn.Flatten(), nn.Linear(CONTEXT * 96, width)]
            if layer_norm:
                layers.append(nn.LayerNorm(width))
            layers.extend([nn.ReLU(), nn.Dropout(0.1), nn.Linear(width, width)])
            if layer_norm:
                layers.append(nn.LayerNorm(width))
            layers.extend([nn.ReLU(), nn.Linear(width, 1)])
            self.net = nn.Sequential(*layers)

        def forward(self, value):
            return self.net((value - self.mean) / self.std)

    return Head()


def predict_features(model, values: np.ndarray) -> np.ndarray:
    """Применить голову ко всем 80-мс окнам с ограниченным размером батча."""
    import torch

    model.eval()
    scores = []
    with torch.no_grad():
        for start in range(0, len(values), 256):
            scores.append(
                torch.sigmoid(model(torch.from_numpy(values[start : start + 256])))
                .numpy()
                .reshape(-1)
            )
    return np.concatenate(scores)


def balanced_bag_order(positive_count: int, negative_count: int, rng) -> np.ndarray:
    """Cover every negative once, replay positives to avoid drowning them in TTS."""
    if positive_count < 1 or negative_count < 1:
        raise ValueError("для балансировки нужны обе метки")
    negative = rng.permutation(negative_count) + positive_count
    batches = []
    for start in range(0, len(negative), 8):
        batch_negative = negative[start : start + 8]
        positive = rng.choice(positive_count, len(batch_negative), replace=True)
        batch = np.concatenate([positive, batch_negative])
        rng.shuffle(batch)
        batches.append(batch)
    return np.concatenate(batches)


def train_head(
    features: dict,
    output: Path,
    epochs: int,
    seed: int,
    init_from: Path | None = None,
    learning_rate: float = 0.001,
    balanced: bool = False,
    confirmed_loss: bool = False,
    width: int = 32,
    layer_norm: bool = True,
):
    """Обучить по train; выбрать checkpoint и порог по val, не обращаясь к test."""
    import torch

    torch.manual_seed(seed)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    rng = np.random.default_rng(seed)
    positives = [record["features"] for record in features["train"]["positive"]]
    negatives = []
    for record in features["train"]["negative"]:
        values = record["features"]
        if record.get("whole_bag"):
            negatives.append(values)
            continue
        # Negative-bag покрывает ~4.8 с; любой положительный score внутри ошибочен.
        negatives.extend(values[first : first + 60] for first in range(0, len(values), 60))
    bags = positives + negatives
    positive_masks = [
        record.get("positive_frame_mask", np.ones(len(record["features"]), dtype=bool))
        for record in features["train"]["positive"]
    ]
    labels = np.asarray([1.0] * len(positives) + [0.0] * len(negatives), dtype=np.float32)
    frames = np.concatenate([bag[:, -1, :] for bag in bags])
    mean = frames.mean(axis=0).astype(np.float32)
    std = np.maximum(frames.std(axis=0), 0.1).astype(np.float32)
    model = build_model(mean, std, width, layer_norm)
    if init_from:
        # Includes the old mean/std buffers: do not silently change the feature
        # scale under an already trained network. Only tensors, not pickled code.
        model.load_state_dict(
            torch.load(init_from / "firaya_trial.pt", map_location="cpu", weights_only=True),
            strict=True,
        )
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=0.01)
    best, state = None, None
    history = []
    # Epoch zero is a candidate too, so fine-tuning cannot degrade the chosen
    # validation objective relative to its initial weights on the NEW val set.
    for epoch in range(0 if init_from else 1, epochs + 1):
        model.train()
        total_loss = 0
        order = (
            balanced_bag_order(len(positives), len(negatives), rng)
            if balanced
            else rng.permutation(len(bags))
        )
        if epoch == 0:
            order = np.asarray([], dtype=int)
        for first in range(0, len(order), 16):
            selected = order[first : first + 16]
            length = max(len(bags[index]) for index in selected)
            batch = np.zeros((len(selected), length, CONTEXT, 96), dtype=np.float32)
            valid = np.zeros((len(selected), length), dtype=bool)
            positive_valid = np.zeros_like(valid)
            for index, number in enumerate(selected):
                batch[index, : len(bags[number])] = bags[number]
                valid[index, : len(bags[number])] = True
                positive_valid[index, : len(bags[number])] = (
                    positive_masks[number] if number < len(positives) else True
                )
            optimizer.zero_grad()
            logits = model(torch.from_numpy(batch.reshape(-1, CONTEXT, 96)))
            logits = logits.reshape(len(selected), length)
            # Weak labels: не считаем все окна positive, выбираем лучшее в каждой записи.
            permitted = torch.from_numpy(valid & positive_valid)
            logits = logits.masked_fill(~permitted, -1e4)
            if confirmed_loss:
                # Train the SAME two-frame confirmation used by the runtime.
                # An isolated high frame cannot satisfy a positive label.
                logits = torch.minimum(logits[:, :-1], logits[:, 1:])
            logits = logits.max(dim=1).values
            target = torch.from_numpy(labels[selected])
            weights = torch.where(target == 0, 2.0, 1.0)
            loss = torch.nn.functional.binary_cross_entropy_with_logits(
                logits, target, weight=weights
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            total_loss += float(loss.detach())
        if epoch == 1 or epoch % 5 == 0 or epoch == epochs or confirmed_loss:
            positive_scores = [
                predict_features(model, item["features"]) for item in features["val"]["positive"]
            ]
            negative_scores = [
                predict_features(model, item["features"]) for item in features["val"]["negative"]
            ]
            metric = choose_threshold(positive_scores, negative_scores)
            metric.update(epoch=epoch, training_loss=total_loss)
            key = (metric["false_events"] > 0, metric["false_events"], -metric["recall"])
            if best is None or key < best["key"]:
                best = {"key": key, **metric}
                state = copy.deepcopy(model.state_dict())
            history.append(metric)
            print(
                f"epoch {epoch}: val {metric['hits']}/{metric['total']}; "
                f"false {metric['false_events']}; threshold {metric['threshold']:.4f}",
                flush=True,
            )
    model.load_state_dict(state)
    model.eval()
    torch.save(model.state_dict(), output / "firaya_trial.pt")
    (output / "training_history.json").write_text(json.dumps(history, indent=2))
    return model, {key: value for key, value in best.items() if key != "key"}


def export_onnx(model, output: Path) -> float:
    """Экспортировать совместимую openWakeWord голову и проверить численное совпадение."""
    import onnx
    import onnxruntime as ort
    import torch

    probability_model = torch.nn.Sequential(model, torch.nn.Sigmoid()).eval()
    sample = torch.zeros(1, CONTEXT, 96)
    torch.onnx.export(
        probability_model,
        sample,
        str(output / "firaya_trial.onnx"),
        input_names=["audio_features"],
        output_names=["firaya"],
        dynamic_axes={"audio_features": {0: "batch"}, "firaya": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    onnx.checker.check_model(onnx.load(str(output / "firaya_trial.onnx")))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        str(output / "firaya_trial.onnx"), options, providers=["CPUExecutionProvider"]
    )
    values = np.random.default_rng(17).normal(size=(20, CONTEXT, 96)).astype(np.float32)
    expected = predict_features(model, values)
    actual = session.run(None, {"audio_features": values})[0].reshape(-1)
    error = float(np.max(np.abs(expected - actual)))
    if error > 1e-5:
        raise RuntimeError(f"ONNX/PyTorch mismatch: {error}")
    return error


def evaluate(model, features: dict, threshold: float) -> tuple[dict, list[dict], list[dict]]:
    """Оценить зафиксированную модель на положительных файлах и непрерывном фоне."""
    predictions, background = [], []
    for record in features["positive"]:
        scores = predict_features(model, record["features"])
        row = record["row"]
        predictions.append(
            {
                "source": row["source"],
                "speaker": Path(row["source"]).parts[2],
                "condition": Path(row["source"]).name.rsplit("_", 1)[0],
                "confirmed_peak": max_confirmed_score(scores),
                "detected": max_confirmed_score(scores) >= threshold,
            }
        )
    for record in features["negative"]:
        scores = predict_features(model, record["features"])
        events = event_indices(scores, threshold)
        background.append(
            {
                "source": record["source"],
                "evaluated_seconds": record["evaluated_seconds"],
                "false_events": len(events),
                "confirmed_peak": max_confirmed_score(scores),
                "event_seconds": [PREFIX + (index + 1) * FRAME / RATE for index in events],
            }
        )
    groups = {}
    for condition in sorted({row["condition"] for row in predictions}):
        group = [row for row in predictions if row["condition"] == condition]
        hits = sum(row["detected"] for row in group)
        groups[condition] = {"detected": hits, "total": len(group), "recall": hits / len(group)}
    hits = sum(row["detected"] for row in predictions)
    seconds = sum(row["evaluated_seconds"] for row in background)
    false = sum(row["false_events"] for row in background)
    metric = {
        "detected": hits,
        "total": len(predictions),
        "recall": hits / len(predictions),
        "by_condition": groups,
        "background_seconds": seconds,
        "false_events": false,
        "false_events_per_hour": false / (seconds / 3600),
    }
    if false == 0:
        upper = float(-np.log(0.05) / (seconds / 3600))
        metric["zero_events_poisson_95_percent_upper_bound_per_hour"] = upper
    return metric, predictions, background


def main(argv: list[str] | None = None) -> int:
    """Создать отдельный опыт; не менять рабочую модель, ROS или исходный набор."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA / "curated/reviewed_01")
    parser.add_argument("--raw-root", type=Path, default=DATA / "raw")
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reuse-features", type=Path)
    parser.add_argument(
        "--synthetic", type=Path, help="добавить подтверждённый Silero pilot ФирАя (4 голоса)"
    )
    parser.add_argument("--reuse-synthetic-features", type=Path)
    parser.add_argument("--init-from", type=Path, help="дообучать PT-веса этого опыта")
    parser.add_argument("--learning-rate", type=float)
    parser.add_argument("--balanced-batches", action="store_true")
    parser.add_argument("--confirmed-loss", action="store_true")
    parser.add_argument("--head-width", type=int, default=32)
    parser.add_argument("--no-layer-norm", action="store_true")
    parser.add_argument(
        "--context-augmentation",
        action="store_true",
        help="имя из real train/val внутри речи; парные отрицательные примеры",
    )
    parser.add_argument("--reuse-context-features", type=Path)
    parser.add_argument("--val-speaker", default="s03")
    parser.add_argument("--test-speaker", default="s04")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--seed", type=int, default=1337)
    args = parser.parse_args(argv)
    if args.epochs < 1:
        parser.error("--epochs должен быть положительным")
    if args.head_width < 1:
        parser.error("--head-width должен быть положительным")
    learning_rate = (
        args.learning_rate
        if args.learning_rate is not None
        else (0.0001 if args.init_from else 0.001)
    )
    if not np.isfinite(learning_rate) or learning_rate <= 0:
        parser.error("--learning-rate должен быть положительным и конечным")
    if args.reuse_synthetic_features and not args.synthetic:
        parser.error("--reuse-synthetic-features требует --synthetic")
    if args.context_augmentation and not args.synthetic:
        parser.error("--context-augmentation требует --synthetic")
    if args.reuse_context_features and not args.context_augmentation:
        parser.error("--reuse-context-features требует --context-augmentation")
    rows = [
        json.loads(line) for line in (args.dataset / "manifest.jsonl").read_text().splitlines()
    ]
    plan = plan_split(rows, args.val_speaker, args.test_speaker)
    initialization = None
    if args.init_from:
        previous = json.loads((args.init_from / "feature_provenance.json").read_text())
        if json.loads((args.init_from / "split_plan.json").read_text()) != plan or previous[
            "backbone"
        ] != json.loads((args.backbone / "provenance.json").read_text()):
            parser.error("начальные веса относятся к другому real split/backbone")
        initial_result = json.loads((args.init_from / "results.json").read_text())
        if initial_result.get("head_width", 32) != args.head_width or initial_result.get(
            "layer_norm", True
        ) != (not args.no_layer_norm):
            parser.error("архитектура начальных весов не совпадает с --head-width/--no-layer-norm")
        initialization = {
            "experiment": str(args.init_from),
            "checkpoint_sha256": hashlib.sha256(
                (args.init_from / "firaya_trial.pt").read_bytes()
            ).hexdigest(),
        }
    synthetic_plan, synthetic_rows = None, []
    if args.synthetic:
        try:
            from .wakeword_synthetic_training import add_synthetic, load_synthetic
        except ImportError:
            from wakeword_synthetic_training import add_synthetic, load_synthetic
        synthetic_plan, synthetic_rows = load_synthetic(args.synthetic)
    if args.reuse_features:
        args.reuse_features = resolve_feature_cache(args.reuse_features)
        previous_plan = json.loads((args.reuse_features / "split_plan.json").read_text())
        if previous_plan != plan:
            parser.error("кэш относится к другому набору или разделению данных")
    output = args.output
    if output is None:
        number = 1
        prefix = "silero_mixed" if args.synthetic else "real_only"
        while (DATA / f"experiments/{prefix}_{number:02d}").exists():
            number += 1
        output = DATA / f"experiments/{prefix}_{number:02d}"
    output.mkdir(parents=True, exist_ok=False)
    print(f"Опыт: {output}", flush=True)
    (output / "split_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    paths = {args.raw_root / row["source"] for row in rows}
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    for row in rows:
        if before[str(args.raw_root / row["source"])] != row["source_sha256"]:
            raise ValueError(f"изменён исходник: {row['source']}")
    started = time.monotonic()
    features = prepare_features(
        plan, args.raw_root, output, args.backbone, args.seed, args.reuse_features
    )
    # Preserve real-only evaluation: do not combine TTS seconds with real background.
    real_features = {
        split: {kind: list(records) for kind, records in groups.items()}
        for split, groups in features.items()
    }
    added = None
    if args.synthetic:
        (output / "synthetic_plan.json").write_text(
            json.dumps(
                {
                    "source_root": str(args.synthetic),
                    "plan": synthetic_plan,
                    "manifest_sha256": hashlib.sha256(
                        (args.synthetic / "manifest.jsonl").read_bytes()
                    ).hexdigest(),
                    "label_policy": "User confirmed ФирАя and four pronunciation-preview voices; "
                    "pilot labels derived from synthesis text, not individually reviewed.",
                    "validation_policy": "Whole negative text/word families withheld across all "
                    "voices/rates; "
                    "auxiliary only, shared TTS engine/voices, not real holdout.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        added = add_synthetic(
            features,
            synthetic_rows,
            args.synthetic,
            args.raw_root,
            plan,
            output,
            args.backbone,
            args.seed,
            args.reuse_synthetic_features,
        )
    contextual = None
    if args.context_augmentation:
        try:
            from .wakeword_context_training import add_context
        except ImportError:
            from wakeword_context_training import add_context
        contextual = add_context(
            features,
            plan,
            args.raw_root,
            args.synthetic,
            synthetic_rows,
            output,
            args.backbone,
            args.seed,
            args.reuse_context_features,
        )
    (output / "feature_provenance.json").write_text(
        json.dumps(
            {
                "reused_from": str(args.reuse_features) if args.reuse_features else None,
                "backbone": json.loads((args.backbone / "provenance.json").read_text()),
                "seed": args.seed,
                "warmup_seconds": PREFIX,
                "tail_seconds": TAIL,
                "synthetic_features": "synthetic_augmentation.json" if args.synthetic else None,
                "context_features": "context_augmentation.json" if contextual else None,
            },
            indent=2,
        )
    )
    model, selection = train_head(
        features,
        output,
        args.epochs,
        args.seed,
        args.init_from,
        learning_rate,
        args.balanced_batches,
        args.confirmed_loss,
        args.head_width,
        not args.no_layer_norm,
    )
    # TEST впервые используется моделью только после завершения выбора checkpoint/порога.
    export_error = export_onnx(model, output)
    metrics, positive_predictions, background = {}, [], []
    for split in ("train", "val", "test"):
        metric, positive, negative = evaluate(model, real_features[split], selection["threshold"])
        metrics[split] = metric
        positive_predictions.extend({"split": split, **row} for row in positive)
        background.extend({"split": split, **row} for row in negative)
    unchanged = all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in before.items()
    )
    if not unchanged:
        raise RuntimeError("исходные WAV изменились во время опыта")
    result = {
        "synthetic_speech_used": bool(args.synthetic),
        "seed": args.seed,
        "initialization": initialization,
        "learning_rate": learning_rate,
        "balanced_batches": args.balanced_batches,
        "confirmed_pair_loss": args.confirmed_loss,
        "head_width": args.head_width,
        "layer_norm": not args.no_layer_norm,
        "context_counts": {
            split: {kind: len(records) for kind, records in groups.items()}
            for split, groups in contextual.items()
        }
        if contextual
        else None,
        "model": f"openWakeWord 0.6 frozen features + custom {args.head_width}-unit DNN, weak "
        f"file-level MIL",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("openwakeword", "torch", "onnxruntime", "onnx", "numpy")
        },
        "selection": selection,
        "metrics": metrics,
        "streaming_frame_ms": 80,
        "consecutive_frames": 2,
        "cooldown_seconds": 2.0,
        "onnx_max_absolute_error": export_error,
        "raw_hashes_unchanged": unchanged,
        "elapsed_seconds": time.monotonic() - started,
        "ready_for_production": False,
        "limitations": [
            "Один validation-голос и один test-голос; только около 5 минут фона на split.",
            "Оценка positive на уровне файла: время произнесения не размечено независимо.",
            "До/после positive добавлен фон из того же split для streaming-контекста.",
            "Три фоновые записи из одной сессии: независимость реальных окружений не доказана.",
            "Результат диагностический; ROS backend и production-конфигурация не изменены.",
            "Прежний real test уже использовался в прошлом опыте; нужна новая независимая запись.",
        ],
    }
    if added is not None:
        result["synthetic_counts"] = {
            split: {kind: len(records) for kind, records in groups.items()}
            for split, groups in added.items()
        }
        result["synthetic_diagnostics"] = {}
        for split, groups in added.items():
            pos = [
                max_confirmed_score(predict_features(model, item["features"]))
                for item in groups["positive"]
            ]
            neg = [predict_features(model, item["features"]) for item in groups["negative"]]
            result["synthetic_diagnostics"][split] = {
                "positive_hits": sum(peak >= selection["threshold"] for peak in pos),
                "positive_total": len(pos),
                "negative_total": len(neg),
                "negative_files_triggered": sum(
                    bool(event_indices(score, selection["threshold"])) for score in neg
                ),
                "false_events": sum(
                    len(event_indices(score, selection["threshold"])) for score in neg
                ),
            }
        result["limitations"].append(
            "Silero auxiliary validation не независима по голосам/синтезатору. "
            "Синтез не имитирует аппаратную обработку XVF и не гарантирует перенос на человека."
        )
    (output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (output / "background_predictions.json").write_text(
        json.dumps(background, ensure_ascii=False, indent=2)
    )
    with (output / "positive_predictions.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(positive_predictions[0]))
        writer.writeheader()
        writer.writerows(positive_predictions)
    (output / "inference_config.json").write_text(
        json.dumps(
            {
                "model": "firaya_trial.onnx",
                "threshold": selection["threshold"],
                "consecutive_frames": 2,
                "cooldown_seconds": 2.0,
                "feature_frames": CONTEXT,
                "feature_dim": 96,
                "production": False,
            },
            indent=2,
        )
    )
    table = []
    for split, metric in metrics.items():
        for condition, value in metric["by_condition"].items():
            table.append(
                f"<tr><td>{split}</td><td>{html.escape(condition)}</td>"
                f"<td>{value['detected']}/{value['total']}</td></tr>"
            )
    report_title = (
        "Реальные записи + Silero ФирАя" if args.synthetic else "Реальные записи, без синтеза"
    )
    report = (
        "<!doctype html><meta charset='utf-8'><title>Пробное обучение Фирая</title>"
        f"<h1>{report_title}</h1>"
        "<p>Не production-модель. Таблица ниже: только реальные записи.</p>"
        "<table border='1'><tr><th>Выборка</th><th>Условие</th><th>Обнаружено</th></tr>"
        + "".join(table)
        + "</table><pre>"
        + html.escape(json.dumps(result, ensure_ascii=False, indent=2))
        + "</pre>"
    )
    (output / "report.html").write_text(report, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
