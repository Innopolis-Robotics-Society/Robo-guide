"""Гистерезис поверх покадровой вероятности речи.

Зачем два порога, а не один. Один порог даёт дребезг на границе: шум
в комнате колеблется вокруг 0.5, и VAD щёлкает active/inactive десятки
раз в секунду. Вход в речь -- по enter_threshold, N окон подряд
(enter_windows, отсекает случайный всплеск на одном окне); выход --
по exit_threshold в течение hangover_ms (не по мгновенному падению
вероятности -- короткий провал внутри фразы не должен рвать высказывание).

min_speech_ms -- НЕ добавляет задержки перед публикацией active=true.
Бюджет barge-in (design §4) считает только enter_windows (64 мс), и это
осознанно: активация публикуется сразу по подтверждению входа, а решение
"это был хлопок, а не речь" принимается ЗАДНИМ ЧИСЛОМ, по факту того,
что сегмент завершился раньше min_speech_ms. Поэтому update() не блокирует
и не откладывает active=true, а возвращает флаг too_short только вместе
с переходом обратно в тишину -- пригодится ровно для диагностики
"ложные срабатывания на тишине" (design §6, шаг 5), а не для управления
таймингом.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["VadHysteresis", "VadResult"]


@dataclass(frozen=True)
class VadResult:
    """Решение гистерезиса для одного окна."""

    active: bool
    state_duration: float
    """Длительность текущего непрерывного состояния (active или нет), сек."""

    segment_ended_too_short: bool = False
    """True ровно на том окне, где сегмент речи завершился короче min_speech_ms."""


class VadHysteresis:
    """Гистерезис активности речи. Не потокобезопасен, как и Scheduler."""

    def __init__(
        self,
        enter_threshold: float = 0.65,
        exit_threshold: float = 0.35,
        enter_windows: int = 2,
        hangover_ms: float = 400.0,
        min_speech_ms: float = 120.0,
        window_ms: float = 32.0,
    ) -> None:
        """Создать гистерезис с параметрами из конфига vad_node."""
        self._enter_threshold = enter_threshold
        self._exit_threshold = exit_threshold
        self._enter_windows = enter_windows
        self._hangover_ms = hangover_ms
        self._min_speech_ms = min_speech_ms
        self._window_ms = window_ms

        self._active = False
        self._state_duration = 0.0
        self._enter_streak = 0
        self._silence_ms = 0.0

    def reset(self) -> None:
        """Сбросить в исходное состояние (тишина). Звать при разрыве потока."""
        self._active = False
        self._state_duration = 0.0
        self._enter_streak = 0
        self._silence_ms = 0.0

    @property
    def active(self) -> bool:
        """Текущее состояние без подачи нового окна."""
        return self._active

    def update(self, probability: float) -> VadResult:
        """Скормить вероятность очередного окна, получить решение."""
        self._state_duration += self._window_ms / 1000.0

        if not self._active:
            return self._update_inactive(probability)
        return self._update_active(probability)

    def _update_inactive(self, probability: float) -> VadResult:
        if probability > self._enter_threshold:
            self._enter_streak += 1
        else:
            self._enter_streak = 0

        if self._enter_streak >= self._enter_windows:
            self._active = True
            self._state_duration = self._enter_streak * self._window_ms / 1000.0
            self._enter_streak = 0
            self._silence_ms = 0.0

        return VadResult(active=self._active, state_duration=self._state_duration)

    def _update_active(self, probability: float) -> VadResult:
        if probability < self._exit_threshold:
            self._silence_ms += self._window_ms
        else:
            self._silence_ms = 0.0

        if self._silence_ms < self._hangover_ms:
            return VadResult(active=True, state_duration=self._state_duration)

        # Хвост тишины (hangover) не в счёт длительности самой речи.
        speech_ms = self._state_duration * 1000.0 - self._silence_ms
        too_short = speech_ms < self._min_speech_ms

        self._active = False
        self._state_duration = self._silence_ms / 1000.0
        self._silence_ms = 0.0

        return VadResult(
            active=False,
            state_duration=self._state_duration,
            segment_ended_too_short=too_short,
        )
