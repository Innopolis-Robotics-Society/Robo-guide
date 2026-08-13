#!/usr/bin/env python3
"""Собрать `config/kb.jsonl` из исходных `.md` в `config/kb_source/`.

DIALOG_REWORK_PLAN.md §3.2: офлайн-скрипт, запускается вручную при правке
текста экскурсовода -- результат коммитится в репозиторий вместе с
исходником, не генерируется на лету при старте `dialog_agent`.

```
cd guide_robot_llm
python3 scripts/build_kb.py
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guide_robot_llm.kb.chunker import chunk_markdown  # noqa: E402 -- см. sys.path выше

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SOURCE_DIR = _PACKAGE_ROOT / "config" / "kb_source"
_DEFAULT_OUTPUT = _PACKAGE_ROOT / "config" / "kb.jsonl"


def build(source_dir: Path, output_path: Path) -> int:
    """Собрать все `.md` из `source_dir` в один `kb.jsonl`. Возвращает код возврата CLI."""
    md_files = sorted(source_dir.glob("*.md"))
    if not md_files:
        print(f"нет .md файлов в {source_dir}", file=sys.stderr)
        return 1

    counter = 0
    with output_path.open("w", encoding="utf-8") as out:
        for md_file in md_files:
            passages = chunk_markdown(md_file.read_text(encoding="utf-8"))
            for passage in passages:
                # id пересчитан по всему корпусу, не только внутри файла --
                # порядок файлов (sorted()) определяет итоговую нумерацию.
                record = {
                    "id": f"kb_{counter:04d}",
                    "heading": passage.heading,
                    "text": passage.text,
                    "location_ids": list(passage.location_ids),
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                counter += 1
    print(f"{counter} пассажей из {len(md_files)} файлов -> {output_path}")
    return 0


def main() -> None:
    """CLI-обвязка."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=_DEFAULT_SOURCE_DIR)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    sys.exit(build(args.source_dir, args.output))


if __name__ == "__main__":
    main()
