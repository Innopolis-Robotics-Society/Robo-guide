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

LiveTTS каждый раз звучит немного иначе, и иногда дубль выходит с выкриком
(окно на 7-10 дБ громче остальной фразы, почти клиппинг). Каждый дубль
меряется: выкрик громче --max-burst-db -- переписать (до --takes дублей,
остаётся самый ровный). Затем уровень клаузы приводится к --level-db, чтобы
клаузы не прыгали по громкости. --recheck меряет уже записанные клаузы и
переписывает только те, где выкрик.

Предупреждения навигации (SayAction в behavior_trees/*.xml: «Внимание,
отойдите...») пишутся громче экскурсии -- --alert-level-db. Текст берётся
прямо из дерева: поменяли фразу в XML -- перезапустить скрипт, новая фраза
запишется сама (старую уберёт --prune). Не понравился дубль конкретной
фразы -- --redo "кусок текста". Сменили уровень -- готовые записи
перемасштабируются без нового синтеза.

Запуск на роботе (ключ -- в .env, как у tts_node):
    set -a; . ./.env; set +a
    python3 scripts/prerecord_tts.py --dry-run   # что будет записано
    python3 scripts/prerecord_tts.py --prune     # записать, убрать устаревшие
    python3 scripts/prerecord_tts.py --recheck   # переписать клаузы с выкриком
    python3 scripts/prerecord_tts.py --redo "отойдите"  # переписать эту фразу
"""

from __future__ import annotations

import argparse
import html
import json
import re
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
from guide_robot_voice.lib.prerecorded import (  # noqa: E402
    Loudness,
    level_to,
    measure_loudness,
    read_wav,
    write_wav,
)

VOICE_CONFIG = ROOT / "guide_robot_voice" / "config"
BT_DIR = ROOT / "guide_robot_navigation" / "behavior_trees"
# Источник предупреждений навигации -- пишутся с --alert-level-db.
ALERT_PREFIX = "bt:"
# Регэкспом, а не XML-парсером: tinyxml2 у bt_navigator терпит «--» в
# комментариях, а xml.etree на них падает.
_SAY_ACTION_RE = re.compile(r"<SayAction\b[^>]*?\btext=\"([^\"]*)\"")
# Расхождение уровня записи с целью, после которого она перемасштабируется.
RELEVEL_TOLERANCE_DB = 0.5
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


def collect_bt_phrases(bt_dir: Path) -> list[tuple[str, str]]:
    """(источник, текст) всех SayAction из деревьев bt_navigator."""
    phrases = []
    for path in sorted(bt_dir.glob("*.xml")):
        for match in _SAY_ACTION_RE.finditer(path.read_text(encoding="utf-8")):
            phrases.append((f"{ALERT_PREFIX}{path.name}", html.unescape(match.group(1))))
    return phrases


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


def best_take(
    backend: YandexBackend, text: str, sample_rate: int, max_burst_db: float, takes: int
) -> tuple[np.ndarray, Loudness, int]:
    """Дубли до первого без выкрика, иначе самый ровный из takes. -> (PCM, громкость, дублей)."""
    best: tuple[np.ndarray, Loudness] | None = None
    for take in range(1, takes + 1):
        pcm = synthesize(backend, text)
        loudness = measure_loudness(pcm, sample_rate)
        if best is None or loudness.burst_db < best[1].burst_db:
            best = (pcm, loudness)
        if loudness.burst_db <= max_burst_db:
            break
        print(f"  выкрик +{loudness.burst_db:.1f} дБ, дубль {take}/{takes}", file=sys.stderr)
    assert best is not None
    return best[0], best[1], take


def needs_retake(path: Path, max_burst_db: float) -> bool:
    """Записанная клауза с выкриком громче порога."""
    pcm, rate = read_wav(path)
    return measure_loudness(pcm, rate).burst_db > max_burst_db


def main() -> int:  # noqa: PLR0912, PLR0915 -- линейный сценарий CLI
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
    parser.add_argument(
        "--recheck", action="store_true", help="переписать уже записанные клаузы с выкриком"
    )
    parser.add_argument(
        "--max-burst-db",
        type=float,
        default=6.0,
        help="самое громкое окно 0.4 с не громче обычного уровня фразы на столько дБ",
    )
    parser.add_argument("--takes", type=int, default=4, help="дублей на клаузу с выкриком")
    parser.add_argument(
        "--level-db", type=float, default=-20.0, help="уровень речи клаузы, дБ полной шкалы"
    )
    parser.add_argument("--no-level", action="store_true", help="не выравнивать громкость")
    parser.add_argument(
        "--max-limit-db",
        type=float,
        default=4.0,
        help="на сколько дБ лимитер может прижать пики, чтобы поднять уровень",
    )
    parser.add_argument(
        "--bt-dir", type=Path, default=BT_DIR, help="деревья bt_navigator с SayAction"
    )
    parser.add_argument(
        "--alert-level-db",
        type=float,
        default=-14.0,
        help="уровень предупреждений навигации (громче экскурсии), дБ полной шкалы",
    )
    parser.add_argument(
        "--redo",
        action="append",
        default=[],
        metavar="ТЕКСТ",
        help="переписать клаузы, содержащие этот кусок текста (повторяемо)",
    )
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
    texts = collect_texts(args.content_dir, args.mission_config, args.text)
    texts += collect_bt_phrases(args.bt_dir)
    for source, text in texts:
        for clause in chunker.split(text):
            key = backend.prerecord_key(clause.text)
            clauses.setdefault(key, {"text": clause.text, "sources": []})["sources"].append(source)

    def target_db(key: str) -> float:
        alert = any(source.startswith(ALERT_PREFIX) for source in clauses[key]["sources"])
        return args.alert_level_db if alert else args.level_db

    redo = [part.casefold() for part in args.redo]

    def wanted(key: str) -> bool:
        path = args.out / f"{key}.wav"
        if args.force or not path.is_file():
            return True
        if any(part in clauses[key]["text"].casefold() for part in redo):
            return True
        return args.recheck and needs_retake(path, args.max_burst_db)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.json"
    previous = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    )

    todo = [key for key in clauses if wanted(key)]
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
    loud: list[str] = []
    try:
        for number, key in enumerate(todo, 1):
            text = clauses[key]["text"]
            pcm, loudness, takes = best_take(
                backend, text, args.sample_rate, args.max_burst_db, args.takes
            )
            if pcm.size == 0 or int(np.abs(pcm.astype(np.int32)).max()) < 1000:  # noqa: PLR2004
                print(f"ОШИБКА: пустой звук для {text!r}", file=sys.stderr)
                return 1
            gain_db = 0.0
            if not args.no_level:
                pcm, gain_db = level_to(
                    pcm,
                    loudness,
                    target_db(key),
                    max_limit_db=args.max_limit_db,
                    sample_rate=args.sample_rate,
                )
            if loudness.burst_db > args.max_burst_db:
                loud.append(f"{key}  +{loudness.burst_db:.1f} дБ  {text}")
            write_wav(args.out / f"{key}.wav", pcm, args.sample_rate)
            clauses[key].update(
                level_db=round(loudness.level_db + gain_db, 1),
                burst_db=round(loudness.burst_db, 1),
                takes=takes,
            )
            seconds = pcm.size / args.sample_rate
            total_s += seconds
            print(
                f"[{number}/{len(todo)}] {seconds:5.1f} с  выкрик +{loudness.burst_db:4.1f} дБ"
                f"  усиление {gain_db:+5.1f} дБ  {text}"
            )
    finally:
        backend.close()
    if loud:
        print(f"выкрик остался после {args.takes} дублей (послушать, поправить текст):")
        for line in loud:
            print(f"  {line}")

    # Уже записанные: перенести замеры из прошлого манифеста и подогнать
    # уровень, если цель сменилась (--level-db/--alert-level-db).
    for key in clauses:
        if key in todo:
            continue
        for field in ("level_db", "burst_db", "takes"):
            if field in previous.get(key, {}):
                clauses[key][field] = previous[key][field]
        if args.no_level:
            continue
        path = args.out / f"{key}.wav"
        pcm, rate = read_wav(path)
        loudness = measure_loudness(pcm, rate)
        if abs(loudness.level_db - target_db(key)) > RELEVEL_TOLERANCE_DB:
            pcm, gain_db = level_to(
                pcm, loudness, target_db(key), max_limit_db=args.max_limit_db, sample_rate=rate
            )
            write_wav(path, pcm, rate)
            clauses[key]["level_db"] = round(loudness.level_db + gain_db, 1)
            print(f"уровень {gain_db:+.1f} дБ без нового синтеза: {clauses[key]['text']}")

    if args.prune:
        for path in stale:
            path.unlink()
    manifest = {key: clauses[key] for key in sorted(clauses)}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: записано {len(todo)} клауз, {total_s / 60:.1f} мин звука")
    return 0


if __name__ == "__main__":
    sys.exit(main())
