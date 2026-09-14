#!/usr/bin/env python3
"""Программная «вторая рецензия» CC-кадров: frame <-> ground-truth конформность.

Кадр читается по пикселям (PIL) и сверяется с GT из cc_scene_gen:
  * цвет в центре каждого объекта = цвет из спеки (с допуском);
  * люди: цвет рубашки в видимой части, цвет окклюдера в перекрываемой;
  * отражение/манекен AUD-CC-001 внутри своих зон, не в людях;
  * кончик указательного жеста вблизи цели;
  * сцены без людей: в «пустых» зонах только стена/пол;
  * текст таблички TOL-CC-007: тёмные пиксеты текста внутри рамки.

Это независимый (от build_cc_manifest) путь данных: GT -> пиксели,
а не GT -> gold. exit 1 при любом расхождении.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

PKG = Path(__file__).resolve().parents[2]
MEDIA = PKG / "pilot" / "media" / "cc"
GT = PKG / "pilot" / "fixtures" / "cc"

TOL = 25  # допуск на цвет (антиалиасинг/наслоения)


def px(img: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    return tuple(img.getpixel((x, y)))[:3]


def center(box: list[int]) -> tuple[int, int]:
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def near(a: tuple[int, int, int], b: tuple[int, int, int], tol: int = TOL) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def rects_overlap(a: list[int], b: list[int]) -> list[int]:
    return [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]


def sample_point(spec: dict) -> tuple[int, int]:
    """Точка, где заполнение объекта гарантированно = цвет спеки (вне рамки/деталей).

    Центр бокса плох для двух форм: у plant внутренний лист (рисуется выше) накрывает
    центр, у mirror центр может попасть на диагональ/фон отражения.
    """
    x0, y0, x1, y1 = spec["box_px"]
    w, h = x1 - x0, y1 - y0
    shape = spec.get("shape", "rect")
    if shape == "mirror":  # верхний правый угол: вне диагонали, рамки и отражения
        return x1 - w // 4, y0 + h // 8
    if shape == "plant":  # левая кромка внешнего листа: внутренние не накладываются
        return x0 + 8, (y0 + y1 - 50) // 2
    return (x0 + x1) // 2, (y0 + y1) // 2


def check_objects(img, gt, errors):
    for obj_id, spec in gt.get("objects", {}).items():
        c = px(img, *sample_point(spec))
        if not near(c, spec["color"]):
            errors.append(f"{obj_id}: пиксель {c} != цвет спеки {spec['color']}")


SKIN = (232, 184, 155)
LIVE_SHIRTS = ((178, 92, 92), (92, 120, 168), (120, 110, 150), (96, 140, 120),
               (170, 110, 80), (80, 110, 160), (150, 100, 90), (90, 120, 150),
               (120, 90, 130))


def box_distance(pt, box) -> int:
    """0, если точка внутри; иначе мин. расстояние до ближайшей грани."""
    return max(box[0] - pt[0], 0, pt[0] - box[2], box[1] - pt[1], 0, pt[1] - box[3])


def check_pointing(img, gt, errors):
    p = gt.get("pointing")
    if not p:
        return
    tgt = gt["objects"][p["target_id"]]["box_px"]
    if box_distance(tuple(p["tip_px"]), tgt) > 60:
        errors.append(f"кончик жеста {p['tip_px']} далёк от цели {tgt}")


def person_sample(box, occl_box):
    """Точка, где гарантированно человек: голова (если видна) или видимая полоса торса."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    head = ((x0 + x1) // 2, y0 + int(w * 0.26))
    if not occl_box:
        return head, "skin"
    if not (occl_box[0] - 6 <= head[0] <= occl_box[2] + 6
            and occl_box[1] - 6 <= head[1] <= occl_box[3] + 6):
        return head, "skin"
    if occl_box[0] > x0 + 8:  # видна левая полоса
        return (x0 + int(w * 0.30), y0 + int((y1 - y0) * 0.5)), "shirt"
    return (x1 - int(w * 0.30), y0 + int((y1 - y0) * 0.5)), "shirt"


def check_people(img, gt, errors):
    objs = gt.get("objects", {})
    for p in gt.get("people", []):
        note = p.get("note") or ""
        if note.startswith(("reflection", "mannequin")):
            c = px(img, *center(p["box_px"]))
            if any(near(c, s) for s in LIVE_SHIRTS) or near(c, SKIN):
                errors.append(f"{p['person_id']}: «{note}» окрашена как живой человек ({c})")
            continue
        occl_box = objs[p["occluded_by"]]["box_px"] if p.get("occluded_by") else None
        pt, kind = person_sample(p["box_px"], occl_box)
        c = px(img, *pt)
        if kind == "skin" and not near(c, SKIN):
            errors.append(f"{p['person_id']}: в голове пиксель {c}, ожидалась кожа")
        elif kind == "shirt":
            s = c[0] + c[1] + c[2]
            occl_color = objs[p["occluded_by"]]["color"] if p.get("occluded_by") else None
            if (occl_color and near(c, occl_color)) or s > 620 or s < 180:
                errors.append(f"{p['person_id']}: видимая полоса {pt} не торс ({c})")
        if occl_box:
            inter = rects_overlap(p["box_px"], occl_box)
            if inter[2] > inter[0] and inter[3] > inter[1]:
                oc = px(img, (inter[0] + inter[2]) // 2, (inter[1] + inter[3]) // 2)
                if not near(oc, objs[p["occluded_by"]]["color"]):
                    errors.append(f"{p['person_id']}: зона перекрытия {inter} не окклюдером")


def check_plaque_text(img, gt, errors):
    pl = gt.get("plaque")
    if not pl:
        return
    x0, y0, x1, y1 = pl["box_px"]
    dark = sum(1 for x in range(x0, x1, 4) for y in range(y0, y1, 4)
               if px(img, x, y)[0] < 100)
    total = ((x1 - x0) // 4) * ((y1 - y0) // 4)
    if dark / total < 0.03:
        errors.append("в рамке таблички нет тёмного текста")


def main() -> int:
    errors: list[str] = []
    for scene_id in sorted(p.stem for p in GT.glob("*.json")):
        gt = json.loads((GT / f"{scene_id}.json").read_text(encoding="utf-8"))
        img = Image.open(MEDIA / f"{scene_id}.png").convert("RGB")
        if img.size != tuple(gt["size"]):
            errors.append(f"{scene_id}: размер {img.size} != GT {gt['size']}")
            continue
        check_objects(img, gt, errors)
        check_pointing(img, gt, errors)
        check_people(img, gt, errors)
        check_plaque_text(img, gt, errors)

    # сцены «пустоты»: SCN-CC-003 -- только стул, никаких витрин/экранов
    scn3 = Image.open(MEDIA / "SCN-CC-003.png").convert("RGB")
    gt3 = json.loads((GT / "SCN-CC-003.json").read_text(encoding="utf-8"))
    if list(gt3["objects"]) != ["chair-01"]:
        errors.append("SCN-CC-003: в GT не только стул")
    c = px(scn3, 400, 300)  # левая стена
    if c[0] + c[1] + c[2] < 500:
        errors.append(f"SCN-CC-003: стена не светлая {c}")

    if errors:
        print(f"PIXEL CHECK FAIL ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("PIXEL CHECK OK: все 10 кадров соответствуют GT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
