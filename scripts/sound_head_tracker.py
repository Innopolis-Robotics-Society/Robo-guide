#!/usr/bin/env python3
"""Тест: поворачивать голову FURO-D к источнику звука по DOA с reSpeaker XVF3800.

Читает DOA_VALUE по USB (без pyusb, см. xvf3800_doa.py), переводит пеленг в
угол головы и отправляет пакет позиции по /dev/tty_motors.

Важно, что уже измерено на этом роботе:
  * ноль по энкодеру -- около 1780 (НЕ 1900 из конфига DynRobot);
  * масштаб ~20 единиц на градус;
  * команды меньше ~40-60 единиц (2-3°) голова игнорирует -- мёртвая зона.

Поэтому у скрипта есть --min-step (по умолчанию 3°): мельче он не командует.

Ограничения безопасности:
  * угол головы зажимается в +-limit и в диапазон raw 1550..2360;
    1550 — проверенный текущий минимум контроллера, 2360 — с запасом до упора;
  * пакет валидируется перед записью;
  * --dry-run ничего не отправляет;
  * небольшая компенсация применяется к первой команде по энкодеру;
  * при Ctrl+C голова остаётся там, где стояла (порт просто закрывается).

Примеры:
    # только смотреть, что слышит массив, ничего не двигая
    python3 sound_head_tracker.py --dry-run

    # двигать, знак +1, лимит 8 градусов
    python3 sound_head_tracker.py --sign +1 --limit 8

    # если голова ушла не в ту сторону -- просто перезапустить с --sign -1
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import math
import os
import statistics
import sys
import time
import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from furo_head_capture import QUERY, Decoder, open_com  # noqa: E402
from furo_head_packet import head_packet  # noqa: E402
from furo_head_feedback import HeadFeedback, compensated_target, can_retarget  # noqa: E402
from xvf3800_doa import open_dev, read_doa, read_azimuths  # noqa: E402


def read_head_raw(fd, timeout=1.0):
    """Прочитать текущую позицию головы запросом телеметрии.

    Голова -- это те же 19-байтные кадры, что у колёс: int32 little-endian
    по смещению 14. Возвращает None, если за timeout валидный кадр не пришёл.
    """
    dec = Decoder()
    deadline = time.monotonic() + timeout
    next_write = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_write:
            os.write(fd, QUERY)
            next_write = now + 0.2
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            chunk = b""
        if chunk:
            for frame in dec.feed(chunk):
                return int.from_bytes(frame[14:18], "little", signed=True)
        else:
            time.sleep(0.01)
    return None


def wrap180(deg: float) -> float:
    """Привести угол к диапазону (-180, 180]."""
    return (deg + 180.0) % 360.0 - 180.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zero", type=int, default=1780, help="сырой энкодер, соответствующий 'прямо'")
    ap.add_argument("--units-per-deg", type=float, default=20.0, help="единиц энкодера на градус")
    ap.add_argument("--sign", type=float, default=-1.0, choices=(1.0, -1.0),
                    help="+1/-1: на этом роботе меньший raw = поворот вправо, поэтому -1")
    ap.add_argument("--fov", type=float, default=50.0,
                    help="полуугол пеленга, который отдаётся на полный ход головы; "
                         "дальше -- упор. Меньше fov = резче реакция у центра")
    ap.add_argument("--curve", type=float, default=0.6,
                    help="нелинейность: 1 = линейно внутри fov, <1 = активнее у центра")
    ap.add_argument("--limit", type=float, default=20.0,
                    help="максимальный угол головы, градусы (механика позволяет до ~20)")
    ap.add_argument("--raw-min", type=int, default=1550,
                    help="нижний предел raw (проверенный минимум контроллера)")
    ap.add_argument("--raw-max", type=int, default=2360,
                    help="жёсткий верхний предел raw (измеренный упор 2390 - запас)")
    ap.add_argument("--min-step", type=float, default=3.0, help="минимальный шаг команды, градусы")
    ap.add_argument("--deadband", type=float, default=2.0, help="зона нечувствительности, градусы")
    ap.add_argument("--array-offset", type=float, default=180.0,
                    help="угол DOA, соответствующий 'прямо' робота; на этом роботе ~180")
    ap.add_argument("--mirror", type=float, default=-1.0, choices=(1.0, -1.0),
                    help="+1/-1: массив перевёрнут и зеркалит; на этом роботе -1")
    ap.add_argument("--stable", type=float, default=12.0, help="разброс пеленга для доверия, градусы")
    ap.add_argument("--hold-s", type=float, default=0.18,
                    help="сколько СЕКУНД сторона должна держаться, прежде чем двигать")
    ap.add_argument("--cooldown", type=float, default=0.6,
                    help="пауза только при СМЕНЕ стороны, чтобы не дёргалась")
    ap.add_argument("--same-side-interval", type=float, default=2.5,
                    help="минимальный интервал между целями с одной стороны, с")
    ap.add_argument("--window", type=int, default=3, help="сколько отсчётов усреднять")
    ap.add_argument("--speed", type=int, default=10, help="raw-скорость в пакете головы")
    ap.add_argument("--port", default="/dev/tty_motors")
    ap.add_argument("--interval", type=float, default=0.04, help="период опроса DOA, с")
    ap.add_argument("--duration", type=float, default=0.0, help="0 = до Ctrl+C")
    ap.add_argument("--home", action="store_true",
                    help="перед началом поставить голову в ноль (позиция известна)")
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять в порт")
    ap.add_argument("--dwell", type=float, default=2.5, help="пауза в --sign-test, с")
    ap.add_argument("--sign-test", action="store_true",
                    help="проверка знака: +A -> 0 -> -A -> 0, потом выход")
    ap.add_argument("--verbose", action="store_true", help="печатать каждый отсчёт")
    ap.add_argument("--trace-doa", action="store_true", help="логировать все пеленги и VAD с временем")
    ap.add_argument("--trace-beams", action="store_true", help="логировать четыре угла лучей AEC")
    args = ap.parse_args()

    if not math.isfinite(args.fov) or args.fov <= 0:
        ap.error("--fov должен быть > 0")
    if not math.isfinite(args.curve) or not 0 < args.curve <= 3:
        ap.error("--curve должен быть в (0, 3]")
    if not math.isfinite(args.limit) or not 0 < args.limit <= 22:
        ap.error("--limit должен быть в (0, 22] -- это меньше измеренного хода")
    if not math.isfinite(args.min_step) or args.min_step <= 0:
        ap.error("--min-step должен быть > 0")
    if not math.isfinite(args.same_side_interval) or args.same_side_interval < 0:
        ap.error("--same-side-interval должен быть >= 0")
    if not math.isfinite(args.units_per_deg) or args.units_per_deg <= 0:
        ap.error("--units-per-deg должен быть > 0")
    if args.window < 1 or not 0 <= args.speed <= 65535:
        ap.error("некорректное окно или скорость")
    if not 0 <= args.raw_min < args.raw_max <= 65535 or not args.raw_min <= args.zero <= args.raw_max:
        ap.error("некорректные пределы или центр вне пределов")

    usb_fd = open_dev()
    ser_fd = None
    stack = contextlib.ExitStack()
    if not args.dry_run:
        try:
            ser_fd = stack.enter_context(open_com(args.port))
        except OSError as exc:
            print(f"не открыть {args.port}: {exc}", file=sys.stderr)
            print("порт занят? проверь: pgrep -af furo_head ; fuser -v /dev/tty_motors", file=sys.stderr)
            os.close(usb_fd)
            return 1

    history: collections.deque[int] = collections.deque(maxlen=args.window)
    sent_deg = 0.0
    last_send = 0.0
    started = time.monotonic()
    moves = 0
    hold_side = 0
    hold_since = 0.0
    sent_side = 0
    feedback = HeadFeedback()
    decoder = Decoder()
    feedback_log_at = 0.0
    beams_log_at = 0.0
    feedback_synced = False

    if args.sign_test:
        # Короткая последовательность, по которой человек глазами определяет знак:
        # куда смотрит голова на первом шаге, тот знак и верный.
        seq = [+args.limit, 0.0, -args.limit, 0.0]
        print(f"тест знака: +{args.limit:g} -> 0 -> -{args.limit:g} -> 0, по 2.5 с на шаг")
        print("смотри, куда повернулась голова на ПЕРВОМ шаге (вправо или влево)\n")
        try:
            for want_deg in seq:
                raw = args.zero + int(round(args.sign * want_deg * args.units_per_deg))
                raw = max(args.raw_min, min(args.raw_max, raw))
                frame = head_packet(raw, args.speed)
                if ser_fd is not None:
                    os.write(ser_fd, frame)
                    state = "отправлено"
                else:
                    state = "dry-run"
                print(f"  голова={want_deg:+5.1f}°  raw={raw:5d}  [{state}]  {frame.hex(' ')}")
                time.sleep(args.dwell)
        except KeyboardInterrupt:
            print("\nпрервано")
        finally:
            os.close(usb_fd)
            stack.close()
        print("\nЕсли на первом шаге голова ушла ВПРАВО -- знак +1 верный.")
        print("Если ВЛЕВО -- перезапусти с --sign -1.")
        return 0

    if args.home and ser_fd is not None:
        # После включения контроллер паркует голову около 1900, а не в наш ноль.
        # Один пакет в ноль синхронизирует физику с логикой sent_deg=0.
        frame = head_packet(args.zero, args.speed)
        os.write(ser_fd, frame)
        print(f"выставление в ноль: raw={args.zero}  {frame.hex(' ')}\n")
        time.sleep(1.5)

    start_raw = read_head_raw(ser_fd) if ser_fd is not None else None
    if start_raw is not None:
        sent_deg = args.sign * (start_raw - args.zero) / args.units_per_deg
        print(f"голова на старте: raw={start_raw} -> {sent_deg:+.1f}° от нуля")
    else:
        print("голова на старте: жду свежую телеметрию")

    print(f"ноль={args.zero}  ед/°={args.units_per_deg:g}  знак={args.sign:+.0f}  "
          f"лимит=±{args.limit:g}°  шаг>={args.min_step:g}°  "
          f"{'DRY-RUN' if args.dry_run else args.port}")
    print("жди vad=1 и держи звук с одной стороны; Ctrl+C для выхода\n")

    def stop_on_term(signum, frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop_on_term)
    try:
        while True:
            if args.duration and time.monotonic() - started >= args.duration:
                break
            now = time.monotonic()
            if ser_fd is not None:
                try:
                    chunk = os.read(ser_fd, 4096)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    frames = decoder.feed(chunk)
                    # Ignore the repeated initial burst and count at most one
                    # observation per loop rather than every buffered frame.
                    if frames and now - started >= 2.0:
                        actual = int.from_bytes(frames[-1][14:18], "little", signed=True)
                        feedback.sample(now, actual)
                position = feedback.position(now)
                if position and not feedback_synced:
                    sent_deg = args.sign * (position[0] - args.zero) / args.units_per_deg
                    feedback_synced = True
                    print(f"свежий энкодер: raw={position[0]:.0f}, "
                          f"позиция={sent_deg:+.1f}°", flush=True)
                if position and now - feedback_log_at >= 0.5:
                    print(f"[{now-started:5.2f}s] feedback_raw={position[0]:.0f} "
                          f"spread={position[1]} target={feedback.target}", flush=True)
                    feedback_log_at = now
            angle, vad = read_doa(usb_fd)
            if args.trace_doa:
                print(f"[{time.monotonic()-started:5.2f}s] doa_raw={angle} vad={vad}", flush=True)
            if args.trace_beams and time.monotonic() - beams_log_at >= 0.25:
                beams = [round(math.degrees(value) % 360, 1) for value in read_azimuths(usb_fd)]
                beams_log_at = time.monotonic()
                print(f"[{beams_log_at-started:5.2f}s] beams={beams} doa={angle} vad={vad}", flush=True)

            if not vad:
                history.clear()
                if args.verbose:
                    print(f"  doa={angle:3d}° vad=0 (тишина)")
                time.sleep(args.interval)
                continue

            history.append(angle)
            if len(history) < args.window:
                time.sleep(args.interval)
                continue

            # круговое среднее, иначе 359° и 1° дают 180°
            rads = [math.radians(a) for a in history]
            mean = math.degrees(math.atan2(sum(map(math.sin, rads)), sum(map(math.cos, rads)))) % 360.0
            spread = max(abs(wrap180(a - mean)) for a in history)
            if spread > args.stable:
                if args.verbose:
                    print(f"  doa={mean:5.1f}° разброс={spread:4.1f}° -- нестабильно, жду")
                time.sleep(args.interval)
                continue

            bearing = wrap180(args.mirror * (mean - args.array_offset))

            # Сторона, а не точный угол: в офисе пеленг гуляет на 20-30 градусов,
            # но "слева или справа" остаётся устойчивым признаком.
            side = 0 if abs(bearing) < args.deadband else (1 if bearing > 0 else -1)
            now = time.monotonic()
            if side != hold_side:
                hold_side, hold_since = side, now
            held = now - hold_since

            # Подтверждение по ВРЕМЕНИ, а не по числу опросов: иначе задержка
            # зависит от частоты опроса и растёт вместе с ней.
            if held < args.hold_s:
                if args.verbose:
                    print(f"  doa={mean:5.1f}° пеленг={bearing:+6.1f}° -- держится {held:.2f}/{args.hold_s:.2f}с")
                time.sleep(args.interval)
                continue

            # Антидребезг только на смену стороны: доворот в ту же сторону
            # не должен ждать, иначе движение ощущается рваным.
            if side != sent_side and now - last_send < args.cooldown and moves:
                if args.verbose:
                    print(f"  doa={mean:5.1f}° пеленг={bearing:+6.1f}° -- антидребезг смены стороны")
                time.sleep(args.interval)
                continue
            if moves and not can_retarget(side, sent_side, now - last_send, args.same_side_interval):
                time.sleep(args.interval)
                continue

            # Линейная карта по всему кругу давала у центра крохи: пеленг +-90
            # при пределе +-20 -- это 0.2 градуса головы на градус звука.
            # Сжимаем вход до +-fov, тогда центральные позиции выбирают весь
            # ход, а крайние упираются в предел (что и нужно).
            u = max(-1.0, min(1.0, bearing / args.fov)) if args.fov > 0 else 0.0
            shaped = math.copysign(abs(u) ** args.curve, u)
            want_deg = args.limit * shaped
            requested_raw = args.zero + int(round(args.sign * want_deg * args.units_per_deg))
            raw = max(args.raw_min, min(args.raw_max, requested_raw))
            effective_deg = args.sign * (raw - args.zero) / args.units_per_deg

            if abs(effective_deg - sent_deg) < args.min_step:
                if args.verbose:
                    print(f"  doa={mean:5.1f}° пеленг={bearing:+6.1f}° цель={want_deg:+5.1f}° "
                          f"-- в мёртвой зоне шага")
                time.sleep(args.interval)
                continue
            if abs(want_deg) < args.deadband and abs(sent_deg) < args.deadband:
                time.sleep(args.interval)
                continue

            if raw != requested_raw:
                print(f"  raw {requested_raw} ограничен до {raw}")
            note = ""
            if ser_fd is not None:
                position = feedback.position(time.monotonic())
                if position is None:
                    print("нет свежих показаний энкодера: команда пропущена", flush=True)
                    time.sleep(args.interval)
                    continue
                command_raw = compensated_target(raw, position[0], args.raw_min, args.raw_max)
                frame = head_packet(command_raw, args.speed)
                if os.write(ser_fd, frame) != len(frame):
                    raise OSError("неполная запись команды головы")
                feedback.command(raw)
                note = f"отправлено {command_raw}, энкодер {position[0]:.0f}"
            else:
                note = "dry-run"
            sent_deg = effective_deg
            sent_side = side
            last_send = time.monotonic()
            moves += 1
            print(f"[{last_send - started:5.2f}s] doa={mean:5.1f}° пеленг={bearing:+6.1f}° "
                  f"-> голова={want_deg:+5.1f}° raw={raw} [{note}]")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nостановлено пользователем")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        os.close(usb_fd)
        stack.close()

    print(f"\nкоманд отправлено: {moves}; последняя заданная цель {sent_deg:+.1f}°")
    return 0


if __name__ == "__main__":
    sys.exit(main())
