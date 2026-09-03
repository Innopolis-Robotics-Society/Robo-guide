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

__all__ = [
    "has_leading_wake_word",
    "has_motion_intent",
    "idle_turn_allowed",
    "looks_like_chit_chat",
    "match_confirm",
    "match_end_tour",
    "match_idle_dismiss",
    "match_start_tour",
    "match_stop_phrase",
    "strip_wake_word",
]

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

# stage2 C3, живой баг 2.4: короткая реплика вида "хорошо, но сначала..."
# укладывается в _MAX_TOKENS_FOR_MATCH и содержит yes-слово ("хорошо") без
# единого слова из _NO_WORDS -- голое пересечение множеств в match_confirm
# схлопывало её в уверенное "да", хотя союз явно откладывает согласие.
# Наличие ЛЮБОГО из этих слов -- сигнал неуверенности сам по себе, вне
# зависимости от длины реплики.
_CONJUNCTION_WORDS = frozenset({"но", "только", "сначала", "потом", "если", "а"})
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

# Похоже на просьбу поехать/провести куда-то -- подстроки, не NLP.
# «повтори»/«привет» сюда не входят намеренно (изначальный живой баг,
# из-за которого регулярка появилась: модель сожгла «повтори» в
# start_tour lab_demo, и робот поехал). Не допуск в ЛЛМ.
# Не гейт безопасности, см. докстринг `has_motion_intent`.
_MOTION_INTENT_RE = re.compile(
    r"экскурс|excursion|\btour\b|\bтур(?:а|у|ом|е|ы|ов)?\b|провед|проводи|отвед"
)

# «вернись домой» / «едем на базу» -- конец тура, не resume.
_GO_HOME_RE = re.compile(
    r"верн\w*\s+дом|ид(?:и|ти)\s+дом|едь\s+дом|поеха\w*\s+дом|"
    r"\bдомой\b|на базу|на старт|return home"
)

# «начни экскурсию» / «проведи тур» -- не просто «экскурсия» в вопросе.
_START_TOUR_RE = re.compile(
    r"(?:начн\w*|начать|start)\w*\s+\w*\s*(?:экскурс|\bтур(?:а|у|ом|е|ы)?\b|\btour\b)"
    r"|(?:экскурс|\bтур(?:а|у|ом|е|ы)?\b|\btour\b)\w*\s+\w*\s*(?:начн|start)"
    r"|провед\w*\s+\w*\s*экскурс"
)

# Токены светской болтовни / приветствия: если ВСЕ слова реплики отсюда --
# моторный tool от модели (guide_to на «привет» без think) не исполняем.
_CHIT_CHAT_TOKENS = frozenset(
    {
        "привет",
        "здравствуй",
        "здравствуйте",
        "добрый",
        "день",
        "вечер",
        "утро",
        "хай",
        "хелло",
        "hello",
        "hi",
        "как",
        "дела",
        "тебя",
        "зовут",
        "имя",
        "спасибо",
        "пожалуйста",
        "пока",
        "хорошо",
        "отлично",
        "круто",
        "супер",
        "ладно",
        "понятно",
        "ясно",
        "ок",
        "окей",
        "угу",
        "ага",
    }
)


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


def has_leading_wake_word(text: str) -> bool:
    """Проверить, что фраза начинается с wake-слова («робот», «робот, ...»)."""
    return bool(_LEADING_WAKE_RE.match(text))


def idle_turn_allowed(raw: str, *, listen_armed: bool) -> bool:
    """Ход к ЛЛМ только после «робот» в фразе или окна после /speech/wakeword.

    «проведи экскурсию» без wake-слова -- нет. Стоп-фразы сюда не входят.
    """
    text = strip_wake_word(raw)
    if not text:
        return False
    return listen_armed or has_leading_wake_word(raw)


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
    """Разобрать ответ на «Идём дальше?»/`ask_visitor` (stage2 C2). `None` -- неуверенно, к ЛЛМ.

    Любое слово из `_CONJUNCTION_WORDS` (stage2 C3) -- сигнал неуверенности
    само по себе, независимо от длины реплики: «хорошо, но сначала...»
    иначе схлопывалось бы в уверенное «да» через одно только пересечение
    множеств (живой баг 2.4, см. докстринг `_CONJUNCTION_WORDS`).
    """
    tokens = _tokens(text)
    if not _confident_gate(tokens):
        return None
    if tokens & _CONJUNCTION_WORDS:
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
    if match_end_tour(text):
        return False
    tokens = _tokens(text)
    if not _confident_gate(tokens):
        return False
    return bool(tokens & _STOP_WORDS)


def match_end_tour(text: str) -> bool:
    """«Стоп экскурсия» / «вернись домой» -- END_TOUR, не пропуск остановки.

    `match_stop_phrase` ловит голое «стоп» как SKIP_STOP. Живой баг: «робот
    стоп экскурсия» уезжало на следующую точку; «вернись домой» в ANSWERING
    ушло в finish_answer(outcome=0) и продолжило тур, хотя TTS сказал «домой».
    """
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    if _GO_HOME_RE.search(folded):
        return True
    if not _MOTION_INTENT_RE.search(folded):
        return False
    tokens = _tokens(text)
    if tokens & _STOP_WORDS:
        return True
    return bool(re.search(r"останов|закончи|отмен", folded))


def match_start_tour(text: str) -> bool:
    """Явная просьба начать экскурсию -- не «что за экскурсия» и не конец тура."""
    if match_end_tour(text):
        return False
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    return bool(_START_TOUR_RE.search(folded))


def has_motion_intent(text: str) -> bool:
    """Проверить, что фраза похожа на просьбу об экскурсии, туре или «отвести к месту».

    Эвристика «похоже на тур/отвести», не допуск в ЛЛМ. Не гейт
    безопасности (stage2 D1): раньше это
    же имя защищало моторные инструменты от chit-chat в `tools/validate.py`
    («повтори» -> start_tour), но правило по подстроке резало и
    подтверждённое через `ask_visitor` «да» посетителя. Эта защита теперь
    у `tool_broker_node.call_tool()`'s `confirmed` (`CallTool.srv`), не
    здесь. Пустая строка -- не намерение.
    """
    # ponytail: несколько подстрок, словарь если появятся ложные отказы.
    folded = unicodedata.normalize("NFC", text).lower().replace("ё", "е")
    return bool(text.strip()) and bool(_MOTION_INTENT_RE.search(folded))


def looks_like_chit_chat(text: str) -> bool:
    """Все токены -- приветствие/светская болтовня (без просьбы что-то сделать).

    Уже гейт в `dialog/turn.py`: моторный tool на такой реплике не
    исполняется (живой баг -- guide_to на «привет» после урезки think).
    Не пересекается с «да» на ask_visitor: тот путь не идёт через
    execute_tool свежего guide_to из фазы действия.
    """
    tokens = _tokens(text)
    return bool(tokens) and tokens <= _CHIT_CHAT_TOKENS


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
