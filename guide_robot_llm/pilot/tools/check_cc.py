#!/usr/bin/env python3
"""Сверка CC-трека пилота: manifest против контракта пилота.

Независимые источники истины:
  * A.3 таблица из docs/pilot_skillset_and_vlm_candidates.md (зафиксирована
    здесь литералом) vs проекция allowed_tools() из build_cc_manifest.py
    (зеркало frozen tools/schema.py, хеш проверяется);
  * ground truth из cc_scene_gen (pilot/fixtures/cc/*.json);
  * каталог-фикстура pilot/fixtures/catalog_cc.json (whitelist id).

Не nullo: exit 1 при любом нарушении. --tamper --field <json-path> —
инъекция битого значения в ВРЕМЕННУЮ копию (самопроверка чекаера).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]  # guide_robot_llm/
PILOT = PKG / "pilot"
SCHEMA_PY = PKG / "guide_robot_llm" / "tools" / "schema.py"
SCHEMA_FREEZE_SHA = "fe6572685cffa38d3297070a1219f754b85575b924972408fede88e0d5d51d87"

# A.3 (док) — побуквенная таблица allowed_tools(state, llm_only=True)
A3: dict[str, list[str]] = {
    "IDLE": ["reply", "tell_about", "ask_visitor", "lookup_content", "search_content",
             "resolve_location", "start_tour", "guide_to", "tour_by_points"],
    "GREETING": ["reply", "stop_tour", "ask_visitor", "lookup_content", "search_content",
                 "resolve_location", "guide_to"],
    "NAVIGATING": ["reply", "stop_tour", "hold_position", "ask_visitor", "lookup_content",
                   "search_content", "resolve_location", "guide_to"],
    "NARRATING": ["reply", "stop_tour", "pause", "ask_visitor", "lookup_content",
                  "search_content", "resolve_location", "guide_to"],
    "ANSWERING": ["reply", "stop_tour", "finish_answer", "ask_visitor", "lookup_content",
                  "search_content", "resolve_location", "guide_to"],
    "AWAITING_CONFIRM": ["reply", "stop_tour", "confirm", "ask_visitor", "lookup_content",
                         "search_content", "resolve_location", "guide_to"],
    "PAUSED": ["reply", "stop_tour", "resume", "ask_visitor", "lookup_content",
               "search_content", "resolve_location", "guide_to"],
    "HELD": ["reply", "stop_tour", "ask_visitor", "lookup_content", "search_content",
             "resolve_location", "guide_to"],
    "RETURNING": ["reply", "stop_tour", "ask_visitor", "lookup_content", "search_content",
                  "resolve_location", "guide_to"],
}
STATES = tuple(A3)
MOTION = {"start_tour", "guide_to", "tour_by_points"}
TRACK_GOLD = {"pointing": {"target_box"}, "scene": {"claims", "unanswerable"},
              "audience": {"count"}, "tool": {"action", "abstention"}}
TEMPLATE_KEYS = {"episode_id", "track", "source_family", "media", "split_group_id",
                 "utterance", "mission_snapshot", "gold", "review", "not_observed"}
NOT_OBSERVED = {"robot_pose", "lidar", "sonar", "camera_calibration", "latency"}


def fail(errors: list[str], msg: str) -> None:
    errors.append(msg)


def count_per_rule(people: list[dict]) -> int:
    return sum(1 for p in people
               if p.get("counts_as_person") and p["visible_fraction"] >= 0.5)


def check(manifest: Path, ledger: Path, catalog: Path, errors: list[str]) -> int:
    sys.path.insert(0, str(PILOT / "tools"))
    from build_cc_manifest import allowed_tools  # noqa: PLC0415

    # 0. freeze-хеш schema.py (иначе A.3-проекция недоверенна)
    if hashlib.sha256(SCHEMA_PY.read_bytes()).hexdigest() != SCHEMA_FREEZE_SHA:
        fail(errors, "tools/schema.py отличается от freeze-хеша — A.3 устарел")

    # 1. проекция == A.3 для всех 9 состояний
    for state in STATES:
        if allowed_tools(state) != A3[state]:
            fail(errors, f"A.3-рассогласование state={state}: {allowed_tools(state)} != {A3[state]}")

    rows = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
    cc = [r for r in rows if r.get("source_family") == "controlled"]
    if len(cc) != 15:
        fail(errors, f"CC-эпизодов {len(cc)}, ожидается 15")

    catalog_data = json.loads(catalog.read_text(encoding="utf-8"))
    tour_ids = {t["tour_id"] for t in catalog_data["tours"]}
    location_ids = {l["location_id"] for l in catalog_data["locations"]}

    ledger_media: set[str] = set()
    with ledger.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if row and row[0] != "media_id":
                ledger_media.add(row[0])

    scene_of: dict[str, str] = {}
    group_of_media: dict[str, set[str]] = {}
    media_of_group: dict[str, set[str]] = {}

    for r in cc:
        eid = r["episode_id"]
        # 2. поля шаблона
        missing = TEMPLATE_KEYS - set(r)
        if missing:
            fail(errors, f"{eid}: нет полей шаблона {sorted(missing)}")
        if set(r["not_observed"]) != NOT_OBSERVED:
            fail(errors, f"{eid}: not_observed не тот набор")
        if r["utterance"]["language"] != "ru":
            fail(errors, f"{eid}: язык реплики не ru")
        if r["media"]["sha256"] != hashlib.sha256((PKG / r["media"]["uri"]).read_bytes()).hexdigest():
            fail(errors, f"{eid}: sha256 медиа не сходится")
        if r["media"]["media_id"] not in ledger_media:
            fail(errors, f"{eid}: строка прав для {r['media']['media_id']} отсутствует")

        # 3. группировка: одна картинка = одна группа (и обратно)
        group = r["split_group_id"]
        group_of_media.setdefault(group, set()).add(r["media"]["media_id"])
        media_of_group.setdefault(r["media"]["media_id"], set()).add(group)
    for g, medias in group_of_media.items():
        if len(medias) > 1:
            fail(errors, f"группа {g} содержит несколько картинок: {sorted(medias)}")
    for m, groups in media_of_group.items():
        if len(groups) > 1:
            fail(errors, f"картинка {m} разнесена по группам: {sorted(groups)}")
    for r in cc:
        eid = r["episode_id"]

        snap = r["mission_snapshot"]
        state = snap["state"]
        if state not in A3:
            fail(errors, f"{eid}: неизвестное состояние {state}")
            continue
        # 4. allowed_tools == A.3 (два независимых источника)
        if snap["allowed_tools"] != A3[state]:
            fail(errors, f"{eid}: allowed_tools != A.3[{state}]")

        gold = r["gold"]
        gtype = gold["type"]
        if gtype not in TRACK_GOLD[r["track"]]:
            fail(errors, f"{eid}: gold.type={gtype} недопустим для трека {r['track']}")

        # 5. золото против ground truth / каталога
        scene = r["media"]["media_id"].removeprefix("CC-")
        scene_of[eid] = scene
        gt = json.loads((PILOT / "fixtures" / "cc" / f"{scene}.json").read_text(encoding="utf-8"))
        if gtype == "target_box":
            t = gold["target"]
            if t["candidate_id"] not in snap["visible_candidate_ids"]:
                fail(errors, f"{eid}: цель {t['candidate_id']} не среди кандидатов")
            if gt["objects"][t["candidate_id"]]["box_px"] != t["box_px"]:
                fail(errors, f"{eid}: box_px цели не совпадает с GT")
            for d in t["distractors"]:
                if d == t["candidate_id"] or d not in snap["visible_candidate_ids"]:
                    fail(errors, f"{eid}: дистрактор {d} некорректен")
        elif gtype == "claims":
            if not (1 <= len(gold["claims"]) <= 6):
                fail(errors, f"{eid}: атомарных фраз {len(gold['claims'])} (нужно <= 6)")
        elif gtype == "unanswerable":
            if not gold.get("reason"):
                fail(errors, f"{eid}: unanswerable без причины")
        elif gtype == "count":
            expect = count_per_rule(gt.get("people", []))
            if gold["count"] != expect:
                fail(errors, f"{eid}: count={gold['count']}, по правилу из GT {expect}")
        elif gtype == "action":
            tool = gold["tool"]
            if tool not in A3[state]:
                fail(errors, f"{eid}: gold-инструмент {tool} не разрешён в {state}")
            if tool == "start_tour" and gold["args"]["tour_id"] not in tour_ids:
                fail(errors, f"{eid}: tour_id вне каталога")
            if tool == "guide_to" and gold["args"]["location_id"] not in location_ids:
                fail(errors, f"{eid}: location_id вне каталога")
            if tool == "ask_visitor":
                on_yes = gold["args"]["on_yes"]
                if on_yes["tool"] == "ask_visitor":
                    fail(errors, f"{eid}: on_yes нельзя вести в ask_visitor")
                if on_yes["tool"] == "guide_to" and on_yes["args"]["location_id"] not in location_ids:
                    fail(errors, f"{eid}: on_yes guide_to вне каталога")
        elif gtype == "abstention":
            if not gold.get("reason"):
                fail(errors, f"{eid}: abstention без причины")
            if r["episode_id"] == "TOL-CC-007":
                pass  # motion и так не gold; проверено типом

    # 6. candidates ⊆ каталога
    for r in cc:
        snap = r["mission_snapshot"]
        unknown = set(snap["visible_candidate_ids"]) - {e["content_id"] for e in catalog_data["exhibits"]}
        if unknown:
            fail(errors, f"{r['episode_id']}: кандидаты вне каталога {sorted(unknown)}")

    # 7. TOL-CC-007: motion никогда не gold
    for r in cc:
        if r["episode_id"] == "TOL-CC-007" and r["gold"]["type"] == "action" \
                and r["gold"]["tool"] in MOTION:
            fail(errors, "TOL-CC-007: motion не может быть gold")
    return len(cc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tamper", choices=["sha256", "tools"], default=None,
                    help="самопроверка: подменить значение в копии и убедиться, что чекер ругается")
    args = ap.parse_args()

    manifest = PILOT / "manifest.jsonl"
    if args.tamper:
        # временная копия с инъекцией ошибки
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as tmp:
            rows = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
            for row in rows:
                if row.get("source_family") == "controlled":
                    if args.tamper == "sha256":
                        row["media"]["sha256"] = "0" * 64
                    elif args.tamper == "tools":
                        snap = row["mission_snapshot"]
                        snap["allowed_tools"] = [t for t in snap["allowed_tools"]
                                                 if t != "reply"]
                    break
            tmp.write("".join(json.dumps(r) + "\n" for r in rows))
            tmp_name = tmp.name
        manifest = Path(tmp_name)

    errors: list[str] = []
    try:
        n = check(manifest, PILOT / "rights_ledger.csv",
                  PILOT / "fixtures" / "catalog_cc.json", errors)
    finally:
        if args.tamper:
            manifest.unlink(missing_ok=True)

    if errors:
        print(f"CHECK FAIL ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"CC CHECK OK: {n} episodes valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
