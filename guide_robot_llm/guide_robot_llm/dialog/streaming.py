"""Потоковая озвучка ответа: предложения уходят в TTS, пока LLM ещё генерирует.

Без этого робот молчит, пока модель не допишет ответ целиком (а в inline-ходе
ещё и закрывающие скобки JSON). Здесь растущий текст режется по границам
предложений: первое готовое открывает Say-цель, остальные дописываются в неё
же (`SpeechStream`), так что реплика остаётся одной целью -- без паузы на
старт новой и с одним `spoken_text` для истории.

Модуль без rclpy: транспорт (`SpeechStream`) даёт вызывающий.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol

from guide_robot_llm.dialog.sanitize import sanitize_answer

__all__ = ["SentenceStreamer", "SpeechStream", "extract_reply_text"]

# Конец предложения, за которым уже видно начало следующего. Требование
# заглавной/цифры/кавычки после пробела отсекает «т. е.», «им. Попова» и
# обрыв на недописанном токене.
_BOUNDARY_RE = re.compile(r"[.!?…]+[»\"')\]]*\s+(?=[«\"(\[A-ZА-ЯЁ0-9—–-])")
# Сокращения, после которых идёт имя собственное: «им. Попова», «ул. Ленина».
_ABBREVIATIONS = frozenset(
    {"им", "ул", "г", "гг", "пр", "просп", "д", "св", "проф", "акад"}
    | {"т", "см", "стр", "рис", "ок"}
)
_WORD_BEFORE_RE = re.compile(r"(\w+)$")
_TOOL_JSON_RE = re.compile(r'\{\s*"tool"\s*:')
_REPLY_TOOL_RE = re.compile(r'"tool"\s*:\s*"reply"')
_ARGS_TEXT_RE = re.compile(r'"args"\s*:\s*\{[^{}]*?"text"\s*:\s*"')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class ToolResultLike(Protocol):
    """Итог Say (см. `turn.ToolResultLike`)."""

    ok: bool
    message: str
    data: dict


class SpeechStream(Protocol):
    """Одна потоковая Say-цель."""

    def say_first(self, text: str) -> None:
        """Открыть цель первым куском, не дожидаясь её конца."""

    def push(self, text: str) -> None:
        """Дописать кусок в открытую цель."""

    def close(self, tail: str) -> ToolResultLike:
        """Последний кусок (может быть пустым) + final; дождаться итога цели."""

    def cancel(self) -> None:
        """Снять цель: ход прерван."""


def extract_reply_text(raw: str) -> str | None:
    """Декодированное (пока, может быть, недописанное) `args.text` из `{"tool":"reply",...}`.

    `None` -- пока нельзя сказать, что это reply с текстом: `tool` ещё не
    пришёл (или не reply), либо `args.text` ещё не начался. Недописанная
    escape-последовательность в хвосте не декодируется -- дождётся следующего
    дельта-чанка.
    """
    tool = _REPLY_TOOL_RE.search(raw)
    if tool is None:
        return None
    match = _ARGS_TEXT_RE.search(raw, tool.end())
    if match is None:
        return None
    out: list[str] = []
    index = match.end()
    while index < len(raw):
        char = raw[index]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            index += 1
            continue
        if index + 1 >= len(raw):
            break
        code = raw[index + 1]
        if code == "u":
            digits = raw[index + 2 : index + 6]
            if len(digits) < 4:
                break
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                break
            index += 6
            continue
        out.append(_ESCAPES.get(code, code))
        index += 2
    return "".join(out)


class SentenceStreamer:
    """Кормится дельтами LLM, отдаёт готовые предложения в `SpeechStream`.

    `extract` превращает сырой накопленный текст в текст реплики (`None` --
    ещё рано). Каждый кусок проходит `sanitize_answer`; общий бюджет --
    `max_chars`, как у нестримленного ответа. Если кусок чистится в пусто
    (модель начала JSON вместо текста), поток замораживается: дальше ничего
    не озвучивается.
    """

    def __init__(
        self,
        stream: SpeechStream,
        *,
        extract: Callable[[str], str | None] = lambda raw: raw,
        max_chars: int = 400,
        on_start: Callable[[], None] = lambda: None,
        check_aborted: Callable[[], bool] = lambda: False,
    ) -> None:
        """`on_start` зовётся перед первым звуком; `check_aborted` -- там же."""
        self._stream = stream
        self._extract = extract
        self._max_chars = max_chars
        self._on_start = on_start
        self._check_aborted = check_aborted
        self._raw = ""
        self._consumed = 0
        self._spoken: list[str] = []
        self._frozen = False
        self.started = False

    @property
    def spoken_text(self) -> str:
        """Всё, что уже отдано в речь."""
        return " ".join(self._spoken)

    def on_delta(self, piece: str) -> None:
        """Очередной content-чанк LLM."""
        self._raw += piece
        if self._frozen:
            return
        text = self._extract(self._raw)
        if text is None:
            return
        last = None
        for match in _BOUNDARY_RE.finditer(text, self._consumed):
            if not _is_abbreviation(text, match):
                last = match
        if last is None:
            return
        chunk = text[self._consumed : last.end()]
        self._consumed = last.end()
        self._emit(chunk)

    def restart(self) -> None:
        """Лестница бэкендов начала новую попытку: текст пойдёт заново.

        До первого звука -- просто начать сначала. После -- второй ответ
        поверх первого не склеить, поэтому звучит только уже сказанное.
        """
        if self.started:
            self._frozen = True
            return
        self._raw = ""
        self._consumed = 0

    def finish(self, final_raw: str | None = None) -> tuple[str, ToolResultLike]:
        """Досказать хвост и дождаться итога цели. Только после `started`."""
        tail = ""
        if not self._frozen:
            text = self._extract(self._raw if final_raw is None else final_raw)
            if text is not None and len(text) > self._consumed:
                tail = self._clean(text[self._consumed :])
                if tail:
                    self._spoken.append(tail)
        return self.spoken_text, self._stream.close(tail)

    def abort(self) -> None:
        """Ход прерван: снять цель, если она уже открыта."""
        self._frozen = True
        if self.started:
            self._stream.cancel()

    def _emit(self, chunk: str) -> None:
        clean = self._clean(chunk)
        if not clean:
            return
        if not self.started:
            if self._check_aborted():
                self._frozen = True
                return
            self._on_start()
            self.started = True
            self._spoken.append(clean)
            self._stream.say_first(clean)
            return
        self._spoken.append(clean)
        self._stream.push(clean)

    def _clean(self, chunk: str) -> str:
        """Санитайзер + бюджет; пусто -- заморозить поток."""
        budget = self._max_chars - len(self.spoken_text) - (1 if self._spoken else 0)
        if budget <= 0:
            self._frozen = True
            return ""
        clean = sanitize_answer(chunk, max_chars=budget)
        if not clean or _TOOL_JSON_RE.search(chunk) or len(chunk.strip()) > budget:
            # JSON вместо текста (санитайзер отрезал его вместе с хвостом)
            # или бюджет исчерпан -- после этого куска не говорим ничего.
            self._frozen = True
        return clean


def _is_abbreviation(text: str, match: re.Match[str]) -> bool:
    """Точка после сокращения вида «им.» -- не конец предложения."""
    if text[match.start()] != ".":
        return False
    word = _WORD_BEFORE_RE.search(text, 0, match.start())
    return word is not None and word.group(1).lower() in _ABBREVIATIONS
