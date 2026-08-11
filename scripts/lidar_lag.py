#!/usr/bin/env python3
"""Разложить задержку /scan по стадиям конвейера лидаров.

Считает по бэгу, записанному командой из guide_robot_bringup/README.md
(раздел «Диагностика задержки лидаров»). Работает на том, что штамп
сохраняется по всей цепочке: laser_sector_blanker переиздаёт то же
сообщение, а dual_laser_merger наследует header.stamp первого лидара
(dual_laser_merger.cpp:241). Поэтому сообщения разных стадий можно
джойнить по header.stamp и получить цену каждой стадии, а не только
суммарную задержку.

Читает штампы прямо из CDR: 4 байта инкапсуляции, затем int32 sec и
uint32 nanosec — начало любого сообщения со std_msgs/Header. Ни
rosbag2_py, ни типов сообщений для этого не нужно, скрипт запускается
в любом окружении. /odom (нужен только для угловой скорости)
десериализуется штатно, и если ROS недоступен — колонка градусов просто
пропадает, остальное считается.

Использование:
    python3 scripts/lidar_lag.py <каталог_прогона|каталог_bag|bag_0.db3>
    python3 scripts/lidar_lag.py --self-check
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import statistics as st
import struct
import sys
from pathlib import Path

# Порядок = порядок стадий конвейера, см. lidars.launch.py.
CHAIN = [
    "/scan_left",
    "/scan_left_filtered",
    "/scan_right",
    "/scan_right_filtered",
    "/scan",
]


def header_stamp(data: bytes) -> int:
    """Достать header.stamp из CDR-полезной нагрузки сообщения со Header.

    Возвращает ЦЕЛЫЕ наносекунды. Во float секунды переводить нельзя: при
    epoch ~1.79e9 разрешение float64 около 0.5 мкс, то есть наносекундная
    часть штампа теряется ещё до всякой арифметики.
    """
    sec, nanosec = struct.unpack_from("<iI", data, 4)
    return sec * 1_000_000_000 + nanosec


def find_db(path: Path) -> Path:
    """Принять каталог прогона, каталог bag или сам .db3."""
    if path.is_file():
        return path
    for candidate in (path, path / "bag"):
        files = sorted(candidate.glob("*.db3"))
        if files:
            return files[0]
    raise FileNotFoundError(f"Не нашёл *.db3 в {path}")


def load(db: Path) -> dict[str, list[tuple[int, int, bytes]]]:
    """Вернуть {топик: [(recv_ns, stamp_ns, raw), ...]} для всех записанных топиков.

    Оба времени — целые наносекунды: recv_ns это колонка timestamp rosbag2
    (время приёма рекордером), stamp_ns — header.stamp самого сообщения.
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ids = {name: tid for name, tid in conn.execute("select name,id from topics")}
    out: dict[str, list[tuple[int, int, bytes]]] = {}
    for topic, tid in ids.items():
        rows = conn.execute(
            "select timestamp,data from messages where topic_id=? order by timestamp", (tid,)
        )
        msgs = []
        for recv_ns, blob in rows:
            data = bytes(blob)
            if len(data) < 12:
                continue
            msgs.append((recv_ns, header_stamp(data), data))
        out[topic] = msgs
    return out


def key(stamp_ns: int) -> int:
    """Штамп в целых микросекундах — в этой сетке стадии и сравниваются.

    По наносекундам джойнить нельзя: мерджер гоняет облако через PCL, а
    pcl::PCLHeader.stamp измеряется в МИКРОсекундах, так что round-trip
    fromROSMsg/toROSMsg режет штамп /scan до микросекундной сетки.
    """
    return stamp_ns // 1000


def stats(values: list[float]) -> str:
    """Медиана / p95 / максимум в миллисекундах."""
    if not values:
        return "нет данных"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return f"{st.median(ordered) * 1e3:7.1f} {p95 * 1e3:7.1f} {ordered[-1] * 1e3:7.1f}"


def angular_speeds(msgs: list[tuple[int, int, bytes]]) -> list[tuple[int, float]]:
    """[(recv_ns, |omega|)] из /odom; пустой список, если ROS недоступен."""
    try:
        from nav_msgs.msg import Odometry
        from rclpy.serialization import deserialize_message
    except ImportError:
        return []
    return [
        (recv, abs(deserialize_message(raw, Odometry).twist.twist.angular.z))
        for recv, _stamp, raw in msgs
    ]


def report(bag: dict[str, list[tuple[int, int, bytes]]]) -> None:
    """Напечатать разложение задержки по стадиям."""
    present = [t for t in CHAIN if bag.get(t)]
    if not present:
        raise SystemExit(f"В бэге нет ни одного из {CHAIN}. Записан не тот набор топиков?")

    print("=== Задержка стадии относительно СВОЕГО header.stamp, мс ===")
    print(f"{'топик':26} {'N':>6} {'Гц':>5} {'медиана':>7} {'p95':>7} {'макс':>7}")
    for topic in present:
        msgs = bag[topic]
        span = (msgs[-1][0] - msgs[0][0]) * 1e-9
        rate = (len(msgs) - 1) / span if span > 0 else 0.0
        lat = [(recv - stamp) * 1e-9 for recv, stamp, _ in msgs]
        print(f"{topic:26} {len(msgs):6d} {rate:5.1f} {stats(lat)}")

    print("\n=== Цена отдельной стадии (джойн по header.stamp), мс ===")
    print(f"{'переход':40} {'N':>6} {'медиана':>7} {'p95':>7} {'макс':>7}")
    recv_by_stamp = {t: {key(stamp): recv for recv, stamp, _ in bag.get(t, [])} for t in CHAIN}
    for src, dst in (
        ("/scan_left", "/scan_left_filtered"),
        ("/scan_right", "/scan_right_filtered"),
        ("/scan_left_filtered", "/scan"),
    ):
        shared = recv_by_stamp[src].keys() & recv_by_stamp[dst].keys()
        deltas = [(recv_by_stamp[dst][s] - recv_by_stamp[src][s]) * 1e-9 for s in shared]
        print(f"{src + ' -> ' + dst:40} {len(deltas):6d} {stats(deltas)}")

    # Чей штамп несёт /scan. Мерджер наследует laser_1, но проверить стоит:
    # если совпадений с левым нет, значит laser_1_topic не /scan_left_filtered.
    scan_stamps = set(recv_by_stamp["/scan"])
    if scan_stamps:
        for side in ("left", "right"):
            hits = len(scan_stamps & recv_by_stamp[f"/scan_{side}_filtered"].keys())
            print(f"штамп /scan совпал с /scan_{side}_filtered: {hits}/{len(scan_stamps)}")

    # Рассинхрон лидаров: сколько времени между развёртками, попавшими в один
    # /scan. Мерджер кладёт оба облака в base_footprint через статический TF,
    # то есть эта разница уезжает в скан как поворот одной половины.
    right = sorted(recv_by_stamp["/scan_right_filtered"])
    if scan_stamps and right:
        offsets = []
        for stamp in sorted(scan_stamps):
            i = bisect.bisect_left(right, stamp)
            near = [right[j] for j in (i - 1, i) if 0 <= j < len(right)]
            if near:
                offsets.append(min(abs(stamp - r) for r in near) * 1e-6)  # мкс -> с
        print("\n=== Рассинхрон левого и правого лидара внутри одного /scan ===")
        print(f"{'|stamp_L - stamp_R|':40} {len(offsets):6d} {stats(offsets)}")

        omegas = angular_speeds(bag.get("/odom", []))
        if omegas:
            moving = [w for _, w in omegas if w > 0.15]
            if moving:
                w95 = sorted(moving)[int(0.95 * (len(moving) - 1))]
                phase = st.median(offsets)
                print(
                    f"\nпри omega p95 = {w95:.2f} рад/с это "
                    f"{w95 * phase * 57.2958:.2f} град между половинами /scan,\n"
                    f"плюс {w95 * 0.05 * 57.2958:.2f} град от того, что штамп стоит на "
                    f"НАЧАЛО развёртки (полразвёртки = 50 мс)"
                )
            else:
                print(
                    "\n/odom есть, но вращения выше 0.15 рад/с в бэге нет — "
                    "запишите разворот на месте"
                )
        else:
            print("\n/odom не прочитан (нет ROS в окружении) — колонка градусов пропущена")


def self_check() -> None:
    """Проверить разбор штампа на синтетическом CDR."""
    payload = struct.pack("<4siI", b"\x00\x01\x00\x00", 1786447006, 411363911)
    assert header_stamp(payload) == 1786447006_411363911, header_stamp(payload)
    # Отрицательный sec не встречается в бэгах, но int32 обязан читаться со знаком.
    assert header_stamp(struct.pack("<4siI", b"\x00\x01\x00\x00", -2, 500000000)) == -1_500_000_000
    assert stats([]) == "нет данных"
    assert stats([0.001, 0.002, 0.003]).split()[0] == "2.0"
    # Штампы, различающиеся только наносекундами, обязаны склеиться: PCL режет
    # штамп /scan до микросекунд, иначе стадии не сойдутся ни разу. Именно из-за
    # этого штампы нельзя держать во float секундах — при epoch ~1.79e9
    # наносекундная разница ниже разрешения float64 и склейка становится
    # случайной.
    assert key(1786455540_411363911) == key(1786455540_411363111)
    assert key(1786455540_411363911) != key(1786455540_411364911)
    print("self-check ок")


def main() -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bag", nargs="?", type=Path, help="каталог прогона, каталог bag или .db3")
    parser.add_argument("--self-check", action="store_true", help="проверить разбор CDR и выйти")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return 0
    if args.bag is None:
        parser.error("нужен путь к бэгу или --self-check")
    report(load(find_db(args.bag)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
