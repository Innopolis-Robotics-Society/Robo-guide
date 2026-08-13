"""Санитайзер текста фазы реплики -- единственная защита между моделью и Piper.

Фаза реплики идёт БЕЗ грамматики (свободный русский текст), поэтому ничего
не гарантирует, что модель не начнёт реплику с markdown-разметки или
самопредставления ("Робот:") -- в отличие от фазы действия (GBNF), здесь
единственная защита -- этот модуль, чистая логика без rclpy.

Отдельно защищает от утечки tool-call JSON в речь: на живом прогоне модель
однажды сгенерировала в реплике буквально `{"tool": "starttour", ...}`, и
это ушло в TTS дословно. С инверсией фаз риск только вырос: сырой tool-call
JSON теперь лежит в контексте ПРЯМО ПЕРЕД генерацией реплики, и модели
проще его эхом повторить.
"""

from __future__ import annotations

import json
import re

__all__ = ["sanitize_answer"]

_DEFAULT_MAX_CHARS = 400
_MAX_SELF_INTRO_STRIPS = 3

_SELF_INTRO_RE = re.compile(r"^\s*(ответ|робот|ассистент)\s*:\s*", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^-\s+", re.MULTILINE)
# Парные маркеры *emphasis*/_emphasis_ -- НЕ голое удаление символа: единичное
# подчёркивание внутри id/имени инструмента (snake_case, например
# "start_tour") не парный маркер и не должно пропадать из текста.
_EMPHASIS_PAIR_RE = re.compile(r"(\*{1,2}|_{1,2})(\S(?:.*?\S)?)\1")
_BACKTICK_RE = re.compile("`")
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_END_CHARS = (".", "!", "?", "…")


def sanitize_answer(text: str, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """Снять markdown/самопредставление, схлопнуть переносы, обрезать по границе предложения.

    Порядок важен: markdown/self-intro снимаются ДО схлопывания переносов --
    `_HEADING_RE`/`_BULLET_RE` матчат только в начале строки (`re.MULTILINE`),
    после схлопывания в один пробел этой информации уже не будет.
    """
    without_intro = _strip_self_intro(text)
    without_markdown = _strip_markdown(without_intro)
    collapsed = _WHITESPACE_RE.sub(" ", without_markdown).strip()
    cut = _cut_tool_call_json(collapsed)
    if not cut:
        # Модель сгенерировала форму tool-call вместо реплики (целиком или
        # приклеенную к хвосту текста) -- считаем, что сказать нечего
        # (пустой answer_text), а не озвучиваем JSON дословно.
        return ""
    return _truncate_on_sentence_boundary(cut, max_chars)


def _cut_tool_call_json(text: str) -> str:
    """Отрезать tool-call JSON, где бы он ни начинался в `text`, вместе со всем хвостом.

    Ищем первую позицию `{`, с которой `raw_decode` даёт валидный `dict` с
    ключом `"tool"` -- это форма фазы действия, которой не место в реплике. Не
    голая проверка "весь текст -- JSON": модель может приклеить JSON к
    концу или середине обычного ответа, а не заменить его целиком.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            parsed, _end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "tool" in parsed:
            return text[:start].rstrip()
    return text


def _strip_self_intro(text: str) -> str:
    result = text
    for _ in range(_MAX_SELF_INTRO_STRIPS):
        stripped = _SELF_INTRO_RE.sub("", result, count=1)
        if stripped == result:
            break
        result = stripped
    return result


def _strip_markdown(text: str) -> str:
    without_headings = _HEADING_RE.sub("", text)
    without_bullets = _BULLET_RE.sub("", without_headings)
    without_emphasis = _EMPHASIS_PAIR_RE.sub(r"\2", without_bullets)
    return _BACKTICK_RE.sub("", without_emphasis)


def _truncate_on_sentence_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    last_end = max(window.rfind(ch) for ch in _SENTENCE_END_CHARS)
    if last_end > 0:
        return window[: last_end + 1]
    last_space = window.rfind(" ")
    if last_space > 0:
        return window[:last_space].rstrip()
    return window
