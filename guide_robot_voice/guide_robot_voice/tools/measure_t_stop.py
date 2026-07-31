"""Измерение t_stop: от publish cancel_all до тишины в динамике.

Три режима.

  --list       Показать устройства с числом каналов в каждую сторону.

  --check      Проиграть тон и напечатать фактические параметры открытого
               потока. Пока тон не подтверждён ушами, ни одно число ниже
               не значит ничего.

  --acoustic   Готовит стимул для железного измерения: воспроизводит
               непрерывный тон, через delay секунд шлёт отмену и печатает
               монотонную метку. Собственно t_stop снимается ВНЕШНИМ
               микрофоном, который пишет и щелчок синхронизации, и динамик
               на одну дорожку. Это снимает вопрос о синхронизации часов --
               иначе измеряется в основном рассинхрон.

Без флагов -- софтовый режим: от вызова bump() до возврата abort(),
без звуковой карты. Годится как регрессионный порог в CI, но НЕ является
ответом на вопрос "за сколько робот замолкает": буфер устройства сюда
не входит.

Про частоту и каналы. Устройство открывается через hw:, то есть без
какой-либо конверсии: частота, формат и число каналов обязаны совпадать
с тем, что железо реально умеет. Смотреть здесь:

    cat /proc/asound/card<N>/stream0

Типичный USB Audio Class умеет ровно 48000 и стерео на выходе -- отсюда
умолчания. Моно-сигнал синтезатора размножается по каналам эмиттером.
"""

from __future__ import annotations

import argparse
import statistics
import time

import numpy as np

from guide_robot_voice.audio.devices import describe_devices
from guide_robot_voice.audio.loopback import LoopbackEmitter, analyse_stop, noise_burst
from guide_robot_voice.audio.sink import (
    EpochFencedSink,
    MemoryEmitter,
    SoundDeviceEmitter,
)
from guide_robot_voice.tts.backends import NullBackend

CHARS_PER_SECOND = 14.0


def build_parser() -> argparse.ArgumentParser:
    """Собрать разбор аргументов.

    Вынесено отдельно ради теста. Рассогласование между объявленными
    аргументами и обращениями к args.* -- это AttributeError в рантайме,
    которого не видит ни линтер, ни форматтер.
    """
    parser = argparse.ArgumentParser(description="Измерение t_stop.")
    parser.add_argument("--list", action="store_true", help="показать устройства и выйти")
    parser.add_argument("--check", action="store_true", help="проиграть тон, проверить слышимость")
    parser.add_argument("--acoustic", action="store_true", help="стимул для внешнего замера")
    parser.add_argument(
        "--loopback",
        action="store_true",
        help="автоматический замер: наушник прижать к микрофону той же карты",
    )
    parser.add_argument(
        "--allow-shared",
        action="store_true",
        help="разрешить pulse/default; числа при этом недостоверны",
    )
    parser.add_argument("--device", default=None, help="индекс или подстрока имени, напр. hw:2,0")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--channels", type=int, default=2, help="каналов на выходе устройства")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--max-ms", type=float, default=10.0, help="порог для CI")
    parser.add_argument("--save-wav", default="", help="префикс для сохранения дорожек петли")
    return parser


def _make_sink(args: argparse.Namespace) -> tuple[SoundDeviceEmitter, EpochFencedSink]:
    """Создать эмиттер и сток по аргументам, открыв устройство немедленно."""
    emitter = SoundDeviceEmitter(
        sample_rate=args.sample_rate,
        channels=args.channels,
        block_ms=20,
        device=args.device,
        allow_shared=args.allow_shared,
    )
    sink = EpochFencedSink(emitter, args.sample_rate, max_queue_ms=10_000)
    sink.start()
    return emitter, sink


def _print_device(emitter: SoundDeviceEmitter) -> None:
    """Напечатать фактические параметры открытого потока.

    Аргумент --device и устройство, на котором в итоге пошёл звук, --
    разные сущности. Печатать после открытия, иначе поля пустые.
    """
    for key, value in emitter.info().items():
        print(f"device.{key}={value}")


def _tone_text(seconds: float) -> str:
    """Текст такой длины, чтобы NullBackend выдал нужную длительность."""
    return "x" * max(1, int(seconds * CHARS_PER_SECOND))


def check_audible(args: argparse.Namespace) -> None:
    """Проиграть короткий тон и подтвердить, что звук выходит."""
    emitter, sink = _make_sink(args)
    try:
        _print_device(emitter)
        backend = NullBackend(sample_rate=args.sample_rate, frequency=880.0, amplitude=0.3)
        epoch = sink.epoch
        for block in backend.synthesize(_tone_text(1.5)):
            if not sink.submit(epoch, block):
                break
        sink.wait_idle(epoch, timeout=10.0)
        sink.raise_if_failed()
        print(f"emitted_seconds={sink.played_seconds():.2f}")
        if sink.played_seconds() <= 0.0:
            raise SystemExit("ни одного кадра не доставлено в устройство")
        print("Слышен был тон 880 Гц? Если нет -- измерять t_stop бессмысленно.")
    finally:
        sink.close()


def run_acoustic(args: argparse.Namespace) -> None:
    """Воспроизвести тон и оборвать его, напечатав метку времени."""
    emitter, sink = _make_sink(args)
    try:
        _print_device(emitter)
        backend = NullBackend(sample_rate=args.sample_rate, frequency=1000.0, amplitude=0.4)
        epoch = sink.epoch
        started = time.monotonic()
        for block in backend.synthesize(_tone_text(args.seconds)):
            if not sink.submit(epoch, block):
                break
            if time.monotonic() - started >= args.delay:
                break
        time.sleep(max(0.0, args.delay - (time.monotonic() - started)))

        mark = time.monotonic()
        sink.bump("measure")

        # Порядок принципиален: сначала здоровье стока, потом числа.
        # Сток с неоткрывшимся устройством честно отработает всю логику
        # fencing и выдаст правдоподобные миллисекунды при нулевом звуке.
        sink.raise_if_failed()
        emitted = sink.played_seconds() + sink.metrics.dropped_frames / args.sample_rate
        if emitted <= 0.0:
            raise SystemExit("ни одного сэмпла не отдано в устройство: измерять нечего")

        print(f"cancel_at_monotonic={mark:.6f}")
        print(f"software_t_stop_ms={sink.metrics.t_stop_ms:.2f}")
        print(f"emitted_seconds={emitted:.2f}")
        print("t_stop снимается с внешней записи: щелчок синхронизации -> обрыв тона")
    finally:
        sink.close()


def _save_wav(prefix: str, recording, sample_rate: int) -> None:  # noqa: ANN001
    """Сохранить обе дорожки: числа надо иметь возможность перепроверить глазами."""
    import wave  # noqa: PLC0415

    for name, track in (("out", recording.out), ("in", recording.inp)):
        with wave.open(f"{prefix}_{name}.wav", "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(track.tobytes())


def _print_verdict(result, buffer_ms: float) -> None:  # noqa: ANN001
    """Напечатать вывод, но только если числу можно верить."""
    if not result.valid:
        print("ИЗМЕРЕНИЕ НЕДОСТОВЕРНО:")
        for warning in result.warnings:
            print(f"  - {warning}")
        print("  Прижмите наушник вплотную к микрофону и повторите.")
        print("  Дорожки можно посмотреть глазами: --save-wav /tmp/loopback")
        return
    if result.t_stop_ms > buffer_ms * 0.5:
        print("ВЫВОД: abort() не выбросил буфер устройства -- он доиграл.")
        print("       Уменьшайте buffer_ms и повторяйте замер.")
    else:
        print("ВЫВОД: abort() выбросил буфер, остаток в пределах периода колбэка.")


def run_loopback(args: argparse.Namespace) -> None:
    """Замерить акустический t_stop петлёй на одной карте.

    Одна запись, одна отмена в самом конце. Между фазами bump() не зовём:
    каждая отмена гасит поток вывода, и оси выхода и входа разъезжаются.
    """
    emitter = LoopbackEmitter(
        sample_rate=args.sample_rate,
        out_channels=args.channels,
        block_ms=20,
        device=args.device,
    )
    sink = EpochFencedSink(emitter, args.sample_rate, max_queue_ms=10_000)
    sink.start()
    try:
        for key, value in emitter.info().items():
            print(f"device.{key}={value}")

        epoch = sink.epoch
        rate = args.sample_rate

        # Переходный процесс открытия потоков не должен попасть в анализ.
        sink.submit(epoch, np.zeros(int(rate * 0.3), dtype=np.int16))
        # Калибровочный всплеск: по нему совмещаются оси выхода и входа.
        sink.submit(epoch, noise_burst(int(rate * 0.03)))
        sink.submit(epoch, np.zeros(int(rate * 0.5), dtype=np.int16))

        backend = NullBackend(sample_rate=rate, frequency=1000.0, amplitude=0.4)
        started = time.monotonic()
        for block in backend.synthesize(_tone_text(args.seconds)):
            if not sink.submit(epoch, block):
                break
            if time.monotonic() - started >= args.delay + 0.8:
                break
        time.sleep(max(0.0, args.delay + 0.8 - (time.monotonic() - started)))

        mark_out = emitter.frames_out
        sink.bump("measure")
        # Вывод мёртв, захват продолжает писать -- ради этого хвоста всё и затевалось.
        time.sleep(0.6)
        sink.raise_if_failed()
        recording = emitter.take_recording()

        if args.save_wav:
            _save_wav(args.save_wav, recording, rate)
            print(f"записи сохранены: {args.save_wav}_out.wav, {args.save_wav}_in.wav")

        result = analyse_stop(recording, mark_out, rate)
        print(f"offset_ms={result.offset_ms:.2f}")
        print(f"software_t_stop_ms={sink.metrics.t_stop_ms:.2f}")
        print(f"acoustic_t_stop_ms={result.t_stop_ms:.2f}")
        print(f"buffer_ms_declared={emitter.latency * 1e3:.0f}")
        _print_verdict(result, emitter.latency * 1e3)
    finally:
        sink.close()


def measure_software(iterations: int, sample_rate: int, delay: float) -> list[float]:
    """Прогнать отмену N раз без звуковой карты и вернуть t_stop в мс."""
    results: list[float] = []
    for _ in range(iterations):
        emitter = MemoryEmitter(block=960, interval=0.005)
        sink = EpochFencedSink(emitter, sample_rate, max_queue_ms=10_000)
        sink.start()
        try:
            backend = NullBackend(sample_rate=sample_rate)
            epoch = sink.epoch
            for block in backend.synthesize(_tone_text(10.0)):
                if not sink.submit(epoch, block):
                    break
            time.sleep(delay)
            sink.bump("measure")
            sink.raise_if_failed()
            results.append(sink.metrics.t_stop_ms)
        finally:
            sink.close()
    return results


def main(argv: list[str] | None = None) -> None:
    """Точка входа."""
    args = build_parser().parse_args(argv)

    if args.list:
        print("Вывод:\n" + describe_devices("output"))
        print("Захват:\n" + describe_devices("input"))
        return

    if args.check:
        check_audible(args)
        return

    if args.loopback:
        run_loopback(args)
        return

    if args.acoustic:
        run_acoustic(args)
        return

    samples = measure_software(args.iterations, args.sample_rate, args.delay)
    array = np.array(samples)
    print(f"n={len(samples)}")
    print(f"median={statistics.median(samples):.3f} ms")
    print(f"p95={float(np.percentile(array, 95)):.3f} ms")
    print(f"max={array.max():.3f} ms")
    if array.max() > args.max_ms:
        raise SystemExit(f"софтовый t_stop превысил порог {args.max_ms} мс")


if __name__ == "__main__":
    main()
