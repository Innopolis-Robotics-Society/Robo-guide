#!/usr/bin/env python3
"""Предзаписать тексты экскурсии голосом Yandex SpeechKit для tts_node.

Синтезирует каждую клаузу всех content/*.yaml и фиксированных фраз
mission (приветствие, «Идём дальше?», мост резюме, фраза редиректа) прямо
в WAV -- без микрофона, поэтому в записи только речь, без звука вокруг.
tts_node с prerecorded_dir играет найденные клаузы с диска, без сети
(guide_robot_voice/lib/prerecorded.py).

Клаузы режутся тем же TextChunker, голос и chunker-параметры берутся из
тех же yaml, что у tts_node на роботе (voice.yaml + voice_xvf3800.yaml):
иначе ключи не совпадут и робот пойдёт в облако. Уже записанные клаузы
не синтезируются повторно -- после правки текста достаточно перезапуска.

Запуск на роботе (ключ -- в .env, как у tts_node):
    set -a; . ./.env; set +a
    python3 scripts/prerecord_tts.py --dry-run   # что будет записано
    python3 scripts/prerecord_tts.py --prune     # записать, убрать устаревшие
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guide_robot_voice"))
sys.path.insert(0, str(ROOT / "guide_robot_semantic_map"))
from guide_robot_semantic_map.lib.content_io import load_content_dir  # noqa: E402
from guide_robot_voice.lib.backends import TtsServiceError, YandexBackend  # noqa: E402
from guide_robot_voice.lib.chunker import ChunkerConfig, TextChunker  # noqa: E402
from guide_robot_voice.lib.prerecorded import write_wav  # noqa: E402

VOICE_CONFIG = ROOT / "guide_robot_voice" / "config"
# Значения declare_parameter в tts_node.py -- на случай, если yaml их не задаёт.
TTS_DEFAULTS = {
    "min_chars": 40,
    "max_clause_chars": 180,
    "chars_per_second": 14.0,
    "yandex_voice": "dasha",
    "yandex_role": "",
    "yandex_model": "",
    "yandex_speed": "",
    "yandex_api_key_env": "YANDEX_TTS_API_KEY",
    "yandex_folder_id": "",
}
# Фразы mission_fsm / narration_server, которые уходят в Say как есть.
MISSION_PHRASES = {
    "mission_fsm": ["greeting_text", "confirm_question_text", "redirect_done_phrase"],
    "narration_server": ["resume_bridge_text"],
}
MAX_ATTEMPTS = 3


def ros_params(files: list[Path], node: str) -> dict:
    """Слить ros__parameters узла из yaml по порядку (последний побеждает)."""
    merged: dict = {}
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, section in data.items():
            if key.split("/")[-1] == node and isinstance(section, dict):
                merged.update(section.get("ros__parameters") or {})
    return merged


def collect_texts(
    content_dir: Path, mission_yaml: Path, extra: list[str]
) -> list[tuple[str, str]]:
    """(источник, текст) всего, что экскурсия отдаёт в Say целиком."""
    contents, warnings = load_content_dir(content_dir)
    for warning in warnings:
        print(f"WARN content: {warning}", file=sys.stderr)
    texts = [
        (f"{exhibit_id}.{language}/{chunk.id}", chunk.text)
        for (exhibit_id, language), content in sorted(contents.items())
        for chunk in content.chunks
    ]
    for node, keys in MISSION_PHRASES.items():
        params = ros_params([mission_yaml], node)
        if node == "narration_server" and params.get("resume_bridge_enabled") is False:
            continue
        for key in keys:
            if not params.get(key):
                print(f"WARN {mission_yaml.name}: у {node} не задан {key}", file=sys.stderr)
                continue
            texts.append((f"{node}/{key}", str(params[key])))
    texts.extend(("--text", text) for text in extra)
    return texts


def synthesize(backend: YandexBackend, text: str) -> np.ndarray:
    """Клауза целиком; временный сбой облака -- повтор, отказ ключа -- наверх."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return np.concatenate(list(backend.synthesize(text)))
        except TtsServiceError as error:
            if error.persistent or attempt == MAX_ATTEMPTS:
                raise
            print(f"  сбой ({error}), повтор {attempt}/{MAX_ATTEMPTS - 1}", file=sys.stderr)
            time.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def main() -> int:  # noqa: PLR0915 -- линейный сценарий CLI
    """Собрать клаузы, записать недостающие, по желанию удалить устаревшие."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--voice-config",
        type=Path,
        nargs="+",
        default=[VOICE_CONFIG / "voice.yaml", VOICE_CONFIG / "voice_xvf3800.yaml"],
        help="yaml tts_node в порядке наложения (как в launch)",
    )
    parser.add_argument(
        "--content-dir", type=Path, default=ROOT / "guide_robot_semantic_map" / "content"
    )
    parser.add_argument(
        "--mission-config",
        type=Path,
        default=ROOT / "guide_robot_mission_control" / "config" / "mission.yaml",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "guide_robot_voice" / "prerecorded")
    parser.add_argument(
        "--sample-rate", type=int, default=48000, help="частота записи; tts_node пересчитает"
    )
    parser.add_argument("--text", action="append", default=[], help="ещё фраза (повторяемо)")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что записать")
    parser.add_argument("--prune", action="store_true", help="удалить записи не из этого набора")
    parser.add_argument("--force", action="store_true", help="перезаписать существующие")
    args = parser.parse_args()

    params = {**TTS_DEFAULTS, **ros_params(args.voice_config, "tts_node")}
    chunker = TextChunker(
        ChunkerConfig(
            min_chars=int(params["min_chars"]),
            max_chars=int(params["max_clause_chars"]),
            chars_per_second=float(params["chars_per_second"]),
        )
    )
    backend = YandexBackend(
        voice=str(params["yandex_voice"]),
        role=str(params["yandex_role"]),
        model=str(params["yandex_model"]),
        speed=str(params["yandex_speed"]),
        sample_rate=args.sample_rate,
        api_key_env=str(params["yandex_api_key_env"]),
        folder_id=str(params["yandex_folder_id"]),
        read_timeout_s=30.0,
    )
    print(
        f"голос {params['yandex_voice']}:{params['yandex_role'] or '-'} "
        f"модель {params['yandex_model'] or 'general'} скорость {params['yandex_speed'] or '-'}, "
        f"клаузы {params['min_chars']}..{params['max_clause_chars']} симв."
    )

    clauses: dict[str, dict] = {}
    for source, text in collect_texts(args.content_dir, args.mission_config, args.text):
        for clause in chunker.split(text):
            key = backend.prerecord_key(clause.text)
            clauses.setdefault(key, {"text": clause.text, "sources": []})["sources"].append(source)

    args.out.mkdir(parents=True, exist_ok=True)
    todo = [key for key in clauses if args.force or not (args.out / f"{key}.wav").is_file()]
    chars = sum(len(clauses[key]["text"]) for key in todo)
    print(f"клауз {len(clauses)}, записать {len(todo)} ({chars} символов) -> {args.out}")
    stale = sorted(path for path in args.out.glob("*.wav") if path.stem not in clauses)
    if stale:
        action = "удаляю" if args.prune and not args.dry_run else "не из набора (--prune удалит)"
        print(f"устаревших записей: {len(stale)}, {action}")

    if args.dry_run:
        for key in todo:
            print(f"  {key}  {clauses[key]['text']}")
        return 0

    if todo:
        backend.load()
    total_s = 0.0
    try:
        for number, key in enumerate(todo, 1):
            text = clauses[key]["text"]
            pcm = synthesize(backend, text)
            if pcm.size == 0 or int(np.abs(pcm.astype(np.int32)).max()) < 1000:  # noqa: PLR2004
                print(f"ОШИБКА: пустой звук для {text!r}", file=sys.stderr)
                return 1
            write_wav(args.out / f"{key}.wav", pcm, args.sample_rate)
            seconds = pcm.size / args.sample_rate
            total_s += seconds
            print(f"[{number}/{len(todo)}] {seconds:5.1f} с  {text}")
    finally:
        backend.close()

    if args.prune:
        for path in stale:
            path.unlink()
    manifest = {key: clauses[key] for key in sorted(clauses)}
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"готово: записано {len(todo)} клауз, {total_s / 60:.1f} мин звука")
    return 0


if __name__ == "__main__":
    sys.exit(main())
