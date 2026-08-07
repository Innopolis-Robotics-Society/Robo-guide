"""Инкрементальное клаузное разбиение потока генерации LLM.

Не то же самое, что `guide_robot_voice/lib/chunker.py`: тот режет ГОТОВЫЙ
текст целиком, зная всё, что после точки. Здесь текст приходит кусками
произвольного размера (токенами модели), и решение "это конец предложения"
иногда нельзя принять, пока не пришёл следующий кусок -- например, кто
знает, точка после "т" -- это конец фразы или начало "т.д.", пока не видно
хотя бы одного символа после неё. Поэтому `feed()` может вернуть меньше
клауз, чем реально найдено в буфере: он не отдаёт "подвешенную" точку
в конце буфера, а ждёт следующего вызова. `flush()` в конце хода отдаёт
всё, что накопилось, без дальнейших раздумий.

Список сокращений переиспользован из `guide_robot_voice/lib/chunker.py`
(design §2: "переиспользовать правила ... если модуль импортируем"),
с локальным дублированием на случай отсутствия пакета в окружении теста.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from guide_robot_voice.lib.chunker import _ABBREVIATIONS
except ImportError:
    # Дубликат списка из guide_robot_voice/lib/chunker.py. Намеренно
    # избыточен по той же причине, что и в оригинале: пропуск границы
    # (лишняя длинная клауза) безобиден, ложная граница (разрыв фразы
    # ради озвучки) слышна как неестественная пауза.
    _ABBREVIATIONS: frozenset[str] = frozenset(
        """
        т д п е тт гг вв им др пр см ср рис табл стр илл гл разд прим примеч г гор ул пер пл
        корп кв обл окр респ мин сек ч мес мл мг кг км га тыс млн млрд руб коп проф акад доц
        ст н с к ф м англ лат греч нем фр рус
        """.split()
    )

__all__ = ["SentenceSplitter", "SentenceSplitterConfig"]

_SENTENCE_CHARS = ".!?…"
_TRAILING_WORD = re.compile(r"([А-Яа-яЁёA-Za-z]+)$")
_TRAILING_INITIAL = re.compile(r"(?:^|[\s(—–\"'])[А-ЯЁA-Z]$")
_WHITESPACE = re.compile(r"\s")


@dataclass(frozen=True)
class SentenceSplitterConfig:
    """Пороги разбиения. Значения по умолчанию -- design §7 (`config/llm.yaml`)."""

    max_clause_chars: int = 180
    first_clause_min_chars: int = 24


class SentenceSplitter:
    """Разбивает поток текста на клаузы по мере поступления."""

    def __init__(self, config: SentenceSplitterConfig | None = None) -> None:
        """Создать сплиттер с указанной конфигурацией. Один сплиттер -- один ход."""
        self._cfg = config or SentenceSplitterConfig()
        self._buffer = ""
        self._emitted_any = False

    def feed(self, text: str) -> list[str]:
        """Добавить кусок текста, вернуть список клауз, готовых к озвучке."""
        self._buffer += text
        clauses: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            raw, self._buffer = self._buffer[:cut], self._buffer[cut:]
            clause = raw.strip()
            if clause:
                clauses.append(clause)
                self._emitted_any = True
        return clauses

    def flush(self) -> str | None:
        """Отдать остаток буфера как последнюю клаузу. `None`, если буфер пуст."""
        text = self._buffer.strip()
        self._buffer = ""
        if not text:
            return None
        self._emitted_any = True
        return text

    # -- поиск точки разреза -------------------------------------------------

    def _find_cut(self) -> int | None:
        boundary = self._find_sentence_boundary()
        if boundary is not None:
            return boundary
        if len(self._buffer) >= self._cfg.max_clause_chars:
            forced = self._forced_cut(self._cfg.max_clause_chars)
            if forced is not None:
                return forced
        if not self._emitted_any and len(self._buffer) >= self._cfg.first_clause_min_chars:
            # Ради TTFA первой клаузе не обязательно ждать конца предложения --
            # достаточно набрать first_clause_min_chars и найти границу слова.
            forced = self._forced_cut(self._cfg.first_clause_min_chars)
            if forced is not None:
                return forced
        return None

    def _find_sentence_boundary(self) -> int | None:
        buf = self._buffer
        i, n = 0, len(buf)
        while i < n:
            char = buf[i]
            if char == ";":
                if i + 1 >= n:
                    return None  # нужен хотя бы один символ лукахеда
                return i + 1
            if char not in _SENTENCE_CHARS:
                i += 1
                continue
            j = i
            while j < n and buf[j] in _SENTENCE_CHARS:
                j += 1
            if j >= n:
                return None  # серия точек упирается в конец буфера -- ждём ещё
            if self._is_sentence_boundary(buf, i, j, buf[i:j]):
                return j
            i = j
        return None

    def _is_sentence_boundary(self, text: str, start: int, end: int, run: str) -> bool:
        if run != ".":
            # "!", "?", "...", "?!" -- всегда конец. Многоточие тоже сюда
            # попадает, поскольку run != "." при len(run) > 1.
            return True

        head = text[:start]
        tail = text[end:]

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

    def _forced_cut(self, min_len: int) -> int | None:
        match = _WHITESPACE.search(self._buffer, min_len)
        if match is None:
            return None  # одно длинное слово/URL -- ждём пробела, не рвём его
        return match.start() + 1
