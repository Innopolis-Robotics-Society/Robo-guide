#!/usr/bin/env python3
"""Сформировать офлайн-страницу проверки нарезки без изменения WAV и manifest.

Обновить страницу существующего запуска без загрузки моделей:
  python3 scripts/wakeword_review.py datasets/wakeword/firaya_v1/prepared/run_02

Решения сохраняются в браузере и выгружаются отдельным JSON. «Нет» для
положительной записи отклоняет нарезку, но не меняет метку исходника на negative.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

ASSETS = Path(__file__).with_name("wakeword_review_assets")
DEFAULT_RAW = Path(__file__).resolve().parents[1] / "datasets/wakeword/firaya_v1/raw"


def dataset_fingerprint(rows: list[dict]) -> str:
    """Привязать решения к конкретным исходникам и границам, а не именам WAV."""
    identity = [
        {
            key: row[key]
            for key in (
                "candidate",
                "source",
                "source_sha256",
                "kind",
                "crop_start_sample",
                "crop_end_sample",
            )
        }
        for row in sorted(rows, key=lambda item: item["candidate"])
    ]
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_review_page(output: Path, raw: Path, rows: list[dict], scans: list[dict]) -> None:
    """Пересобрать только HTML: звук, машинный отчёт и решения не трогать."""
    if len({row["candidate"] for row in rows}) != len(rows):
        raise ValueError("повторяющиеся кандидаты в manifest")
    cards = []
    for index, row in enumerate(rows):
        source_url = quote(os.path.relpath(raw / row["source"], output), safe="/")
        candidate_url = quote(row["candidate"], safe="/")
        positive = row["kind"] == "positive"
        question = (
            "Во фрагменте осталось полное слово «Фирая»?"
            if positive
            else "Во фрагменте НЕТ слова «Фирая» и его обрезанной части?"
        )
        accept = "Да, имя целиком" if positive else "Да, имени нет"
        reject = "Нет, плохая нарезка" if positive else "Нет, имя есть / сомневаюсь"
        reasons = html.escape("; ".join(row.get("review_reasons", [])))
        original_start = row["crop_start_seconds"] if not positive else 0
        cards.append(
            f'<details class="review-card" data-index="{index}">'
            f'<summary><span class="badge">Не проверено</span> '
            f'{html.escape(row["source"])}'
            + (
                f' · {row["crop_start_seconds"]:.2f}–{row["crop_end_seconds"]:.2f} с'
                if not positive
                else ""
            )
            + '</summary>'
            f'<p>Границы фрагмента: {row["crop_start_seconds"]:.3f}–'
            f'{row["crop_end_seconds"]:.3f} с; длительность '
            f'{row["candidate_seconds"]:.3f} с. Метод: {html.escape(row["method"])}.</p>'
            f'<div class="comparison"><section><h3>Оригинал — не меняется</h3>'
            f'<audio controls preload="none" data-original-start="{original_start}" '
            f'src="{source_url}"></audio></section>'
            '<section class="candidate"><h3>Фрагмент — принять или отклонить</h3>'
            f'<audio controls preload="none" src="{candidate_url}"></audio>'
            '</section></div>'
            f'<p class="question">{question}</p>'
            '<div class="actions">'
            f'<button type="button" class="accept" data-choice="accepted">{accept}</button>'
            f'<button type="button" class="reject" data-choice="rejected">{reject}</button>'
            '<button type="button" data-choice="pending">Отменить решение</button></div>'
            '<label class="note-label">Заметка (необязательно): '
            '<input class="note" maxlength="2000" placeholder="Например: обрезано начало">'
            '</label>'
            f'<p class="warnings">Автоматические замечания: {reasons or "нет"}</p>'
            f'<p class="transcript">Подсказка ASR (может ошибаться): '
            f'{html.escape(row.get("asr_text") or "нет текста")}</p></details>'
        )
    backgrounds = []
    for scan in scans:
        url = quote(os.path.relpath(raw / scan["source"], output), safe="/")
        backgrounds.append(
            f'<p>{html.escape(scan["source"])}</p>'
            f'<audio controls preload="none" src="{url}"></audio>'
        )
    payload = {
        "schema_version": 1,
        "dataset_fingerprint": dataset_fingerprint(rows),
        "run_name": output.name,
        "rows": rows,
    }
    # JSON внутри script не должен закрыть элемент или стать исполняемым HTML.
    data = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026")
    data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    template = (ASSETS / "review.html").read_text(encoding="utf-8")
    script = (ASSETS / "review.js").read_text(encoding="utf-8")
    page = (
        template.replace("<!-- REVIEW_CARDS -->", "".join(cards))
        .replace("<!-- BACKGROUND_PLAYERS -->", "".join(backgrounds))
        .replace("/* REVIEW_DATA */", data)
        .replace("/* REVIEW_SCRIPT */", script)
        .replace("<!-- RUN_NAME -->", html.escape(output.name))
    )
    (output / "review.html").write_text(page, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Обновить HTML существующего запуска без повторного распознавания."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run", type=Path)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW)
    args = parser.parse_args(argv)
    output = args.run.resolve()
    raw = args.raw_root.resolve()
    rows = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    missing = [row["source"] for row in rows if not (raw / row["source"]).is_file()]
    if missing:
        parser.error(f"не найдены исходники в {raw}; укажите --raw-root")
    scans = json.loads((output / "negative_scan.json").read_text(encoding="utf-8"))
    write_review_page(output, raw, rows, scans)
    print(f"Обновлена страница: {output / 'review.html'}")
    print("WAV, manifest.jsonl и review.csv не изменены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
