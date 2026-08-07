"""Политика конца хода (end-of-turn) для asr_node.

Отдельная библиотека, а не метод ноды -- design §3.4 явно требует эту
границу: интерфейс принимает (partial_text, silence_ms, utterance_ms)
и возвращает bool, и ровно эту сигнатуру позже подменит семантическая
модель, без изменений в остальном коде asr_node.

Три правила, любое достаточно:

  тишина >= base_silence_ms                                   -- базовый путь
  тишина >= short_silence_ms И текст синтаксически завершён    -- быстрый путь
  длительность >= max_utterance_s                              -- страховка

"Синтаксически завершён" на Stage 1 -- эвристика, а не грамматика: длина
>= min_words слов и последнее слово не предлог/союз/вопросительное слово.
Ложное срабатывание (досрочный финал на самом деле не законченной фразы)
это не разрушительно -- narration_server/mission получит транскрипт
чуть короче, чем сказал человек, но не ждёт лишние 250 мс на КАЖДОЙ фразе.
Список функциональных слов -- намеренно с запасом, тем же принципом,
что и список сокращений в chunker.py: пропуск границы (не финализировали
вовремя) безобиден, ложная граница -- тоже, раз есть base_silence_ms как
подстраховка.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["TurnPolicy", "TurnPolicyConfig", "is_syntactically_complete"]

# Предлоги, союзы и вопросительные слова, на которых русская фраза
# практически никогда не заканчивается по-настоящему.
_INCOMPLETE_TRAILING_WORDS: frozenset[str] = frozenset(
    """
    в на с со за из из-за из-под к ко от до по для о об обо у над под перед при
    через без между про на-за без-за близ вокруг внутри вдоль поперёк напротив
    а и но или либо да чтобы что если когда пока хотя как чем будто словно
    потому оттого так итак ведь однако зато также тоже ну же ли
    что кто где когда почему зачем какой какая какие каков сколько куда
    откуда чей чья чьи насколько
    """.split()
)

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def is_syntactically_complete(text: str, min_words: int = 2) -> bool:
    """Эвристика завершённости фразы: длина и последнее слово.

    Не грамматика -- пунктуация не смотрим (партиалы её обычно не несут).
    """
    words = _WORD_RE.findall(text)
    if len(words) < min_words:
        return False
    return words[-1].lower() not in _INCOMPLETE_TRAILING_WORDS


@dataclass(frozen=True)
class TurnPolicyConfig:
    """Пороги политики конца хода. Значения по умолчанию -- design §3.4."""

    base_silence_ms: float = 600.0
    short_silence_ms: float = 350.0
    max_utterance_s: float = 20.0
    min_words_for_short: int = 2


class TurnPolicy:
    """Решает, финализировать ли текущее высказывание."""

    def __init__(self, config: TurnPolicyConfig | None = None) -> None:
        """Создать политику с указанной конфигурацией."""
        self._cfg = config or TurnPolicyConfig()

    @property
    def config(self) -> TurnPolicyConfig:
        """Текущая конфигурация."""
        return self._cfg

    def should_finalize(self, partial_text: str, silence_ms: float, utterance_ms: float) -> bool:
        """Решить, пора ли завершать высказывание."""
        cfg = self._cfg
        if silence_ms >= cfg.base_silence_ms:
            return True
        if silence_ms >= cfg.short_silence_ms and is_syntactically_complete(
            partial_text, cfg.min_words_for_short
        ):
            return True
        return utterance_ms >= cfg.max_utterance_s * 1000.0
