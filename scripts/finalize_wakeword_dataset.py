#!/usr/bin/env python3
"""Применить JSON проверки к новой копии набора, сохранив raw и prepared.

Непроверенные записи по умолчанию НЕ включаются. Флаги --accept-unreviewed-positive
и --trust-negative-sources разрешены только с явным согласием пользователя.
Автоматически принятые тихие фрагменты не помечаются проверенными человеком.
Отклонённые positives исключаются, а не превращаются в negatives.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from .wakeword_review import DEFAULT_RAW, dataset_fingerprint
except ImportError:
    from wakeword_review import DEFAULT_RAW, dataset_fingerprint


def inside(root: Path, name: str) -> Path:
    """Проверить, что путь из manifest не выходит за пределы своей папки."""
    if not isinstance(name, str):
        raise ValueError("путь должен быть строкой")
    path = (root / name).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"путь выходит за пределы {root}: {name}")
    return path


def validate_decisions(rows: list[dict], payload: dict) -> dict:
    """Принять только решения для тех же WAV и тех же границ нарезки."""
    if payload.get("schema_version") != 1:
        raise ValueError("неподдерживаемая версия JSON решений")
    if payload.get("dataset_fingerprint") != dataset_fingerprint(rows):
        raise ValueError("JSON относится к другой версии нарезки")
    known = {row["candidate"] for row in rows}
    if len(known) != len(rows):
        raise ValueError("повторяющиеся кандидаты в manifest")
    decisions = payload.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("нет корректных решений в JSON")
    for name, value in decisions.items():
        if name not in known or not isinstance(value, dict):
            raise ValueError(f"неизвестный кандидат или некорректное решение: {name}")
        if value.get("choice") not in ("accepted", "rejected"):
            raise ValueError(f"неизвестное решение: {name}")
        if not isinstance(value.get("note"), str) or len(value["note"]) > 2000:
            raise ValueError(f"некорректная заметка: {name}")
        try:
            datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"некорректное время решения: {name}") from error
    return decisions


def read_pcm(path: Path) -> bytes:
    """Проверить формат и прочитать PCM без изменения сигнала."""
    with wave.open(str(path), "rb") as recording:
        if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()) != (
            1,
            2,
            16000,
        ):
            raise ValueError(f"неожиданный формат WAV: {path}")
        return recording.readframes(recording.getnframes())


def select_rows(
    rows: list[dict],
    decisions: dict,
    accept_positive: bool,
    trust_negative: bool,
) -> tuple[list[dict], list[dict]]:
    """Отделить включённые записи от отклонённых и ожидающих решения."""
    included, excluded = [], []
    for original in rows:
        row = {**original}
        if row["kind"] not in ("positive", "negative"):
            raise ValueError("неизвестный тип записи")
        decision = decisions.get(row["candidate"])
        choice = decision["choice"] if decision else None
        row["prepared_candidate"] = row["candidate"]
        row["human_decision"] = decision
        row["individually_reviewed"] = bool(decision)
        row["boundary_verified_by_human"] = choice == "accepted" and row["kind"] == "positive"
        row["label_verified"] = False
        if choice == "rejected":
            row.update(
                included=False,
                inclusion_basis="human_rejected",
                exclusion_reason="manual_rejection",
            )
            excluded.append(row)
            continue
        if choice == "accepted":
            basis = "individual_human_review"
        elif row["kind"] == "positive" and accept_positive:
            basis = "user_approved_automatic_crop"
        elif row["kind"] == "negative" and trust_negative:
            basis = "user_attested_negative_source"
        else:
            row.update(
                included=False,
                inclusion_basis="no_inclusion_permission",
                exclusion_reason="not_reviewed",
            )
            excluded.append(row)
            continue
        row.update(
            included=True,
            inclusion_basis=basis,
            label_verified=choice == "accepted" or basis == "user_attested_negative_source",
            source_label_confirmed_by_user=True,
            review_required=False,
            status="included_under_user_policy",
        )
        # Старые автоматические замечания сохраняются; они не являются veto.
        included.append(row)
    return included, excluded


def finalize(
    run: Path,
    raw: Path,
    decisions_path: Path,
    output: Path,
    accept_positive: bool = False,
    trust_negative: bool = False,
) -> dict:
    """Создать новую версию с копиями WAV, ничего не удаляя и не перезаписывая."""
    run, raw, output = run.resolve(), raw.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"папка уже существует: {output}")
    if output == run or run in output.parents or output == raw or raw in output.parents:
        raise ValueError("результат нельзя создавать внутри raw или prepared-запуска")
    rows = [
        json.loads(line)
        for line in (run / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    submitted = decisions_path.read_bytes()
    payload = json.loads(submitted)
    decisions = validate_decisions(rows, payload)
    included, excluded = select_rows(rows, decisions, accept_positive, trust_negative)
    # Проверяем весь запуск до создания результатов, в том числе отклонённые WAV.
    source_pcm = {}
    unchanged = {}
    for row in rows:
        source = inside(raw, row["source"])
        if source not in source_pcm:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            source_pcm[source] = read_pcm(source)
            unchanged[source] = digest
        if unchanged[source] != row["source_sha256"]:
            raise ValueError(f"исходник изменён после подготовки: {source}")
        candidate = inside(run, row["candidate"])
        first, last = row["crop_start_sample"], row["crop_end_sample"]
        if (
            not isinstance(first, int)
            or not isinstance(last, int)
            or not 0 <= first < last <= len(source_pcm[source]) // 2
        ):
            raise ValueError(f"некорректные границы: {candidate}")
        if read_pcm(candidate) != source_pcm[source][first * 2 : last * 2]:
            raise ValueError(f"фрагмент не соответствует исходнику: {candidate}")
        unchanged[candidate] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    for row in included:
        original = inside(run, row["prepared_candidate"])
        parts = Path(row["prepared_candidate"]).parts
        destination_name = str(Path(row["kind"], *parts[1:]))
        destination = inside(output, destination_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        shutil.copyfile(original, destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != unchanged[original]:
            raise RuntimeError(f"ошибка копирования: {destination}")
        row.update(
            candidate=destination_name, file=destination_name, candidate_sha256=unchanged[original]
        )
    changed = [
        str(path)
        for path, digest in unchanged.items()
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest
    ]
    if changed:
        raise RuntimeError(f"данные изменились во время копирования: {changed}")
    policy = {
        "accept_unreviewed_positive": accept_positive,
        "trust_negative_sources": trust_negative,
        "unreviewed_positive_boundaries_are_not_human_verified": True,
        "rejected_positives_are_excluded_not_relabelled": True,
    }
    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "prepared_run": str(run),
        "raw_root": str(raw),
        "output_root": str(output),
        "dataset_fingerprint": dataset_fingerprint(rows),
        "decisions_sha256": hashlib.sha256(submitted).hexdigest(),
        "included": dict(Counter(row["kind"] for row in included)),
        "inclusion_basis": dict(Counter(row["inclusion_basis"] for row in included)),
        "excluded": dict(Counter(row["exclusion_reason"] for row in excluded)),
        "splits": dict(Counter(row["split"] for row in included)),
        "source_hashes_unchanged": True,
        "review_stage_closed": not any(
            row["exclusion_reason"] == "not_reviewed" for row in excluded
        ),
        "training_ready": False,
        "note": "Отбор записей завершён согласно policy; обучение не запускалось. "
        "Для полноценной оценки нужны независимые val/test по людям; "
        "это не выполняется автоматически при отборе.",
    }
    for name, entries in (("manifest.jsonl", included), ("excluded.jsonl", excluded)):
        with (output / name).open("x", encoding="utf-8") as stream:
            for row in entries:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    for name, value in (("summary.json", summary), ("policy.json", policy)):
        with (output / name).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2))
    with (output / "review_decisions.json").open("xb") as stream:
        stream.write(submitted)
    with (output / "selection.csv").open("x", encoding="utf-8", newline="") as stream:
        columns = [
            "source",
            "candidate",
            "kind",
            "included",
            "inclusion_basis",
            "individually_reviewed",
            "boundary_verified_by_human",
            "label_verified",
        ]
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(included + excluded)
    return summary


def main(argv: list[str] | None = None) -> int:
    """Применить решения и явно выбранную политику к новой папке набора."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--accept-unreviewed-positive", action="store_true")
    parser.add_argument("--trust-negative-sources", action="store_true")
    args = parser.parse_args(argv)
    output = args.output
    if output is None:
        parent = args.run.resolve().parent.parent / "curated"
        number = 1
        while (parent / f"reviewed_{number:02d}").exists():
            number += 1
        output = parent / f"reviewed_{number:02d}"
    summary = finalize(
        args.run,
        args.raw_root,
        args.decisions,
        output,
        args.accept_unreviewed_positive,
        args.trust_negative_sources,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
