#!/usr/bin/env python3
"""Собрать заготовку golden-набора из накопленных `interaction_log` jsonl-логов.

DIALOG_REWORK_PLAN.md §9.1. Разметка `expected_*` -- вручную, один раз;
скрипт только готовит черновик с уже проставленным ФАКТИЧЕСКИ наблюдавшимся
`action.tool`/непустотой ответа -- разметка сводится к правке ошибочных
строк, не к вводу заготовки с нуля.

```
python3 scripts/extract_golden.py --log-dir ~/.guide_robot/llm_turns
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_LOG_DIR = Path("~/.guide_robot/llm_turns").expanduser()
_DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "test" / "data" / "turns_golden.jsonl"


def extract(log_dir: Path, output_path: Path) -> int:
    """Прочитать все `*.jsonl` из `log_dir`, написать черновик golden-набора. Код возврата CLI."""
    log_files = sorted(log_dir.glob("*.jsonl"))
    if not log_files:
        print(f"нет jsonl-логов в {log_dir}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_path.open("w", encoding="utf-8") as out:
        for log_file in log_files:
            for raw_line in log_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = record.get("action") or {}
                golden = {
                    "snapshot": record.get("snapshot", {}),
                    "utterance": record.get("utterance", ""),
                    "expected_tool": action.get("tool"),
                    "expected_says_something": bool((record.get("answer_text") or "").strip()),
                }
                out.write(json.dumps(golden, ensure_ascii=False) + "\n")
                count += 1
    print(f"{count} черновых записей из {len(log_files)} файлов -> {output_path}")
    print(
        "Разметка вручную: поправить expected_tool/expected_says_something "
        "там, где ошиблась модель."
    )
    return 0


def main() -> None:
    """CLI-обвязка."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=_DEFAULT_LOG_DIR)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    sys.exit(extract(args.log_dir, args.output))


if __name__ == "__main__":
    main()
