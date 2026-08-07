"""Юниты на гистерезис активности речи."""

from __future__ import annotations

from guide_robot_voice.lib.vad_hysteresis import VadHysteresis

# 32 мс/окно -- 512 сэмплов на 16 кГц (design §3.2).
WINDOW_MS = 32.0


def make(**overrides: object) -> VadHysteresis:
    """Гистерезис с параметрами по умолчанию из design §3.2."""
    defaults = {
        "enter_threshold": 0.65,
        "exit_threshold": 0.35,
        "enter_windows": 2,
        "hangover_ms": 400.0,
        "min_speech_ms": 120.0,
        "window_ms": WINDOW_MS,
    }
    defaults.update(overrides)
    return VadHysteresis(**defaults)  # type: ignore[arg-type]


def feed(vad: VadHysteresis, probability: float, count: int) -> list:
    """Скормить одну и ту же вероятность count раз, вернуть все решения."""
    return [vad.update(probability) for _ in range(count)]


def test_silence_never_activates() -> None:
    """Устойчивая тишина не даёт ни одного false positive."""
    vad = make()
    results = feed(vad, 0.02, 200)
    assert not any(r.active for r in results)


def test_single_spike_does_not_trigger() -> None:
    """Один всплеск вероятности (шум/хлопок) не подтверждён enter_windows."""
    vad = make()
    vad.update(0.02)
    result = vad.update(0.95)
    assert not result.active
    result = vad.update(0.02)
    assert not result.active


def test_sustained_probability_triggers_after_enter_windows() -> None:
    """Два подряд окна выше enter_threshold -- активация ровно на втором."""
    vad = make(enter_windows=2)
    first = vad.update(0.9)
    assert not first.active
    second = vad.update(0.9)
    assert second.active


def test_brief_dip_during_speech_does_not_end_segment() -> None:
    """Короткий провал вероятности внутри фразы не рвёт высказывание.

    hangover_ms=400 -- один провал на одном окне (32 мс) не должен
    закончить сегмент, раз вероятность тут же возвращается выше
    exit_threshold.
    """
    vad = make()
    feed(vad, 0.9, 2)  # активация
    dip = vad.update(0.1)
    assert dip.active
    recovered = vad.update(0.9)
    assert recovered.active


def test_sustained_silence_ends_segment_after_hangover() -> None:
    """Устойчивая тишина в течение hangover_ms завершает сегмент."""
    vad = make(hangover_ms=400.0)
    feed(vad, 0.9, 2)  # активация
    windows_to_exit = 13  # 13*32 = 416 мс >= 400 мс
    results = feed(vad, 0.05, windows_to_exit)
    assert all(r.active for r in results[:-1])
    assert not results[-1].active


def test_short_segment_is_flagged() -> None:
    """Активация, тут же сменившаяся тишиной, -- короче min_speech_ms."""
    vad = make(hangover_ms=400.0, min_speech_ms=120.0)
    feed(vad, 0.9, 2)  # активация: 64 мс речи
    results = feed(vad, 0.05, 13)  # хватает на выход по hangover
    exit_result = results[-1]
    assert not exit_result.active
    assert exit_result.segment_ended_too_short


def test_long_segment_is_not_flagged() -> None:
    """Достаточно долгая речь не считается коротким сегментом."""
    vad = make(hangover_ms=400.0, min_speech_ms=120.0)
    feed(vad, 0.9, 2)  # активация
    feed(vad, 0.9, 10)  # ещё 320 мс уверенной речи
    results = feed(vad, 0.05, 13)  # выход по hangover
    exit_result = results[-1]
    assert not exit_result.active
    assert not exit_result.segment_ended_too_short


def test_state_duration_tracks_current_state() -> None:
    """state_duration растёт, пока состояние не меняется, и считает заново
    с момента перехода -- а не продолжает копить длительность прошлого состояния.
    """
    vad = make()
    feed(vad, 0.9, 2)
    grown = vad.update(0.9)
    assert grown.state_duration > 2 * WINDOW_MS / 1000.0

    exit_results = feed(vad, 0.05, 13)
    on_exit = exit_results[-1]
    assert not on_exit.active
    # На переходе state_duration -- это длительность НОВОГО (неактивного)
    # состояния, то есть хвоста тишины (hangover), а не продолжение счёта
    # активного состояния.
    assert abs(on_exit.state_duration - 13 * WINDOW_MS / 1000.0) < 1e-9

    one_more = vad.update(0.05)
    assert one_more.state_duration > on_exit.state_duration


def test_reset_clears_state() -> None:
    """После reset() гистерезис не тащит состояние прошлого потока."""
    vad = make()
    feed(vad, 0.9, 2)
    assert vad.active
    vad.reset()
    assert not vad.active
    first = vad.update(0.9)
    assert not first.active
