"""Клаузное разбиение текста для потокового синтеза.

Зачем это отдельный модуль, а не три строки в tts_node.

Отмена воспроизведения не может быть быстрее, чем длина уже синтезированного
аудио. Если отдать бэкенду абзац целиком, в буфере окажется полминуты речи,
и никакой epoch-fencing не спасёт: аудио уже создано, память занята, а на
границе клаузы мы получаем управление раз в полминуты. Разбиение задаёт
верхнюю границу на объём "в полёте".

Второе назначение -- смещения символов. Say.Result отдаёт spoken_chars,
по которому narration_server возобновляет монолог с места прерывания.
Без точных границ клауз это поле было бы враньём.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ChunkerConfig", "Clause", "TextChunker"]

# Сокращения, после которых точка не завершает предложение.
# Список намеренно избыточен: ложное срабатывание границы (лишний разрыв)
# слышно как неестественная пауза, пропуск границы -- только как более
# длинная клауза, что безвредно.
_ABBREVIATIONS: frozenset[str] = frozenset(
    """
    т д п е тт гг вв им др пр см ср рис табл стр илл гл разд прим примеч г гор ул пер пл
    корп кв обл окр респ мин сек ч мес мл мг кг км га тыс млн млрд руб коп проф акад доц
    ст н с к ф м англ лат греч нем фр рус
    """.split()
)

_SENTENCE_END = re.compile(r"[.!?\u2026]+")
_CLAUSE_BREAK = re.compile(r"[;:\u2014\u2013]|,(?=\s)")
_TRAILING_INITIAL = re.compile(r"(?:^|[\s(\u2014\u2013\"'])[А-ЯЁA-Z]$")
_TRAILING_WORD = re.compile(r"([А-Яа-яЁёA-Za-z]+)$")
_WHITESPACE = re.compile(r"\s+")

# Минимальный отступ от краёв при принудительном разрезе, символов.
_CUT_MARGIN = 8


@dataclass(frozen=True)
class ChunkerConfig:
    """Параметры разбиения.

    Значения по умолчанию рассчитаны на речь ~14 символов/сек: клауза
    в 160 символов -- это около 11 секунд аудио в худшем случае, что и есть
    верхняя граница на незавершённый синтез.
    """

    min_chars: int = 40
    max_chars: int = 160
    chars_per_second: float = 14.0

    def estimate_seconds(self, text: str) -> float:
        """Оценить длительность произнесения текста."""
        return len(text) / self.chars_per_second


@dataclass(frozen=True)
class Clause:
    """Единица синтеза."""

    text: str
    index: int
    char_start: int
    char_end: int
    terminal: bool
    """Заканчивается ли клауза концом предложения (влияет на просодию)."""


class TextChunker:
    """Разбивает текст на клаузы, пригодные для потокового синтеза."""

    def __init__(self, config: ChunkerConfig | None = None) -> None:
        """Создать чанкер с указанной конфигурацией."""
        self._cfg = config or ChunkerConfig()

    @property
    def config(self) -> ChunkerConfig:
        """Текущая конфигурация."""
        return self._cfg

    def split(self, text: str) -> list[Clause]:
        """Разбить текст на клаузы.

        Границы клауз -- это смещения в исходном тексте, включая пробелы:
        конкатенация text[c.char_start:c.char_end] по всем клаузам
        восстанавливает исходную строку без потерь.
        """
        if not text.strip():
            return []

        spans = self._sentence_spans(text)
        spans = self._enforce_max(text, spans)
        spans = self._merge_short(text, spans)

        clauses: list[Clause] = []
        for index, (start, end) in enumerate(spans):
            chunk = text[start:end]
            clauses.append(
                Clause(
                    text=chunk.strip(),
                    index=index,
                    char_start=start,
                    char_end=end,
                    terminal=bool(chunk.rstrip()) and chunk.rstrip()[-1] in ".!?\u2026",
                )
            )
        return clauses

    # -- границы предложений ------------------------------------------------

    def _sentence_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start = 0
        for match in _SENTENCE_END.finditer(text):
            if not self._is_sentence_boundary(text, match):
                continue
            end = match.end()
            if text[start:end].strip():
                spans.append((start, end))
                start = end
        if text[start:].strip():
            spans.append((start, len(text)))
        return spans

    def _is_sentence_boundary(self, text: str, match: re.Match[str]) -> bool:
        run = match.group()
        if run != ".":
            # "!", "?", "...", "?!" -- всегда конец. Многоточие из трёх точек
            # тоже сюда попадает, поскольку run != ".".
            return True

        head = text[: match.start()]
        tail = text[match.end() :]

        word = _TRAILING_WORD.search(head)
        if word is not None and word.group(1).lower() in _ABBREVIATIONS:
            return False

        if _TRAILING_INITIAL.search(head):
            # "А. С. Пушкин" -- инициалы.
            return False

        if head[-1:].isdigit() and tail[:1].isdigit():
            # Десятичная дробь или нумерация вида 2.1.
            return False

        stripped = tail.lstrip()
        return not (stripped and stripped[0].islower())

    # -- ограничение длины --------------------------------------------------

    def _enforce_max(self, text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        pending = list(spans)
        while pending:
            start, end = pending.pop(0)
            if end - start <= self._cfg.max_chars:
                result.append((start, end))
                continue
            cut = self._best_cut(text, start, end)
            if cut is None:
                # Разрезать не по чему -- одно длинное слово или URL.
                # Оставляем как есть: рвать слово хуже, чем долгая клауза.
                result.append((start, end))
                continue
            pending.insert(0, (cut, end))
            pending.insert(0, (start, cut))
        return result

    def _best_cut(self, text: str, start: int, end: int) -> int | None:
        middle = (start + end) // 2
        low, high = start + _CUT_MARGIN, end - _CUT_MARGIN
        if low >= high:
            return None

        candidates = [m.end() for m in _CLAUSE_BREAK.finditer(text, start, end)]
        candidates = [c for c in candidates if low < c < high]
        if candidates:
            return min(candidates, key=lambda c: abs(c - middle))

        spaces = [start + m.start() for m in _WHITESPACE.finditer(text[start:end])]
        spaces = [c for c in spaces if low < c < high]
        if spaces:
            return min(spaces, key=lambda c: abs(c - middle))

        return None

    # -- склейка коротких ---------------------------------------------------

    def _merge_short(self, text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not spans:
            return spans
        result = [spans[0]]
        for start, end in spans[1:]:
            prev_start, prev_end = result[-1]
            prev_len = len(text[prev_start:prev_end].strip())
            cur_len = len(text[start:end].strip())
            too_short = prev_len < self._cfg.min_chars or cur_len < self._cfg.min_chars
            fits = (end - prev_start) <= self._cfg.max_chars
            if too_short and fits:
                result[-1] = (prev_start, end)
            else:
                result.append((start, end))
        return result
