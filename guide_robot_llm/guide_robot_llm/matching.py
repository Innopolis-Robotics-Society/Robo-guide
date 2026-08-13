"""ASR-фраза -> да/нет/стоп-слово, локально, без ЛЛМ (DIALOG_REWORK_PLAN.md §3.3).

Быстрый путь мимо ЛЛМ для `confirm` ("Идём дальше?") и для "хватит,
дальше" в `ANSWERING` -- риск §9 старого плана: суммарная латентность
ASR -> ЛЛМ -> `submit_confirm` рискует превысить терпение посетителя (>3 с
посетитель повторит ответ). Здесь -- локальное word-level сопоставление по
нормализованному тексту, без внешней модели; неуверенный случай -- `None`/
`False`, вызывающий код (`dialog_agent`, двухфазный ход) решает сам.

Нормализация -- тот же пайплайн, что `guide_robot_semantic_map/lib/
text_norm.py` (NFC, lowercase, ё->е, схлопывание пунктуации), скопирован,
не импортирован -- пакет умышленно не зависит от guide_robot_semantic_map
в рантайме (тот же принцип, что `lib/qos.py`: несовпадающая копия хуже
молчаливого рантайм-импорта чужого пакета только ради одной функции).

`_MAX_TOKENS_FOR_MATCH`/`_QUESTION_WORDS` -- гейт против самого вредного
найденного бага: голое пересечение множеств токенов матчило «а всё-таки
что это такое?» в `ANSWERING` как «хватит» (через слово «всё»/«все»), и
вопрос никогда не доходил до ЛЛМ -- удар ровно по тому состоянию, где
посетитель задаёт вопросы. Быстрый путь существует для односложных
ответов; всё, что длиннее `_MAX_TOKENS_FOR_MATCH` токенов или содержит
вопросительное слово, решает ЛЛМ, а не голое совпадение по множеству.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["match_confirm", "match_idle_dismiss", "match_stop_phrase", "strip_wake_word"]

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_MAX_TOKENS_FOR_MATCH = 3

_QUESTION_WORDS = frozenset(
    {
        "что",
        "как",
        "почему",
        "зачем",
        "откуда",
        "когда",
        "где",
        "кто",
        "какой",
        "какая",
        "какие",
        "разве",
        "неужели",
    }
)

_YES_WORDS = frozenset(
    {
        "да",
        "давай",
        "давайте",
        "поехали",
        "конечно",
        "хорошо",
        "ага",
        "угу",
        "продолжай",
        "продолжайте",
        "идем",
        "идём",
    }
)
_NO_WORDS = frozenset({"нет", "не", "хватит", "стоп", "достаточно"})
# "все"/"всё" умышленно НЕ входят -- слишком частотны в обычной речи
# (см. докстринг модуля про баг с "а всё-таки что это такое?").
_STOP_WORDS = frozenset({"хватит", "дальше", "стоп", "достаточно", "закончили"})

# IDLE-версия стоп-слов -- НАМЕРЕННО отдельный, более узкий набор от
# _STOP_WORDS: "дальше"/"закончили" осмысленны только когда что-то уже
# рассказывается (ANSWERING), а в IDLE рассказывать нечего. Набор дословно
# повторяет стоп-слова guide_robot_voice/wakeword_node.py -- та же лексика,
# на которую посетитель уже привык реагировать как на "прекрати".
_IDLE_DISMISS_WORDS = frozenset({"стоп", "стой", "хватит", "замолчи"})
# Ведущее слово активации ("робот, стоп") никогда не вырезается ASR из
# финального транскрипта -- см. живой баг DIALOG_REWORK_PLAN.md follow-up:
# "робот стоп" в IDLE ушло в ЛЛМ и было принято за существительное.
_WAKE_WORDS = frozenset({"робот"})
_IDLE_DISMISS_IGNORED = _WAKE_WORDS

# Ведущий повтор wake-слова с хвостовой пунктуацией ("Робот, ...", "робот робот ...").
# Анкер в начале строки НАМЕРЕННО: "что такое робот?" -- вопрос про робота,
# слово из середины/конца фразы вырезать нельзя.
_LEADING_WAKE_RE = re.compile(r"^\s*(?:робот\b[\s,.!?—–-]*)+", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    stripped = _NON_WORD.sub(" ", folded)
    normalized = _WHITESPACE.sub(" ", stripped).strip()
    return set(normalized.split()) if normalized else set()


def _confident_gate(tokens: set[str]) -> bool:
    """Проверить, что фраза подходит для быстрого пути (не длинная и не вопрос)."""
    if not tokens or len(tokens) > _MAX_TOKENS_FOR_MATCH:
        return False
    return not (tokens & _QUESTION_WORDS)


def strip_wake_word(text: str) -> str:
    """Срезать ведущее wake-слово ("робот") из финального транскрипта.

    Пустая строка в ответе означает, что реплика состояла ТОЛЬКО из
    wake-слов ("робот", "Робот!", "робот робот") -- содержания нет, ход к
    ЛЛМ запускать не из чего (живой пример: голое "робот" породило полный
    ход, который тут же был убит barge-in-ом). Wake-слово в середине или
    конце фразы ("что такое робот?") не трогается -- это часть содержания.
    """
    tokens = _tokens(text)
    if tokens and tokens <= _WAKE_WORDS:
        return ""
    return _LEADING_WAKE_RE.sub("", text, count=1).strip()


def match_confirm(text: str) -> bool | None:
    """Разобрать ответ на «Идём дальше?». `None` -- неуверенно, передать ЛЛМ."""
    tokens = _tokens(text)
    if not _confident_gate(tokens):
        return None
    has_yes = bool(tokens & _YES_WORDS)
    has_no = bool(tokens & _NO_WORDS)
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


def match_stop_phrase(text: str) -> bool:
    """Проверить, значит ли фраза уверенно «хватит, дальше» (ANSWERING -> SKIP_STOP)."""
    tokens = _tokens(text)
    if not _confident_gate(tokens):
        return False
    return bool(tokens & _STOP_WORDS)


def match_idle_dismiss(text: str) -> bool:
    """Проверить, что фраза в IDLE -- чистая команда отмены без содержания.

    В отличие от `match_stop_phrase` (пересечение множеств -- там
    неуверенный случай уходит к ЛЛМ, цена ошибки мала), здесь проверка на
    ПОДМНОЖЕСТВО: все токены (кроме ведущего "робот") обязаны быть
    стоп-словами. Ложное срабатывание здесь означает молча проглотить
    реальную реплику без единого шанса на ЛЛМ -- цена ошибки выше, чем в
    ANSWERING, поэтому гейт строже: "стоп машина" (лишнее содержание после
    стоп-слова) обязан провалиться и уйти к ЛЛМ, а не быть съеденным молча.
    """
    tokens = _tokens(text) - _IDLE_DISMISS_IGNORED
    if not tokens or len(tokens) > _MAX_TOKENS_FOR_MATCH:
        return False
    return tokens <= _IDLE_DISMISS_WORDS
