"""GBNF-грамматика фазы действия (контракт ADR-0001).

`build_action_grammar(tool_names)` собирает GBNF, в котором модель
выдаёт ТОЛЬКО объект действия контракта:

    {"tool": "<имя>", "args": {...}, "confidence": <0..1>, "abstain": <bool>}

ровно эти 4 поля, в этом порядке, без чужих ключей (включая устаревший
`think`) и без текста до/после объекта. JSON-правила зашиты в теле
(llm_plam.md §4): грамматика задаёт только форму, а не содержимое
аргументов.

`confidence` фиксится в [0, 1] уже на уровне грамматики
(`confidence ::= "1" | "0"."до 12 знаков"`): вне диапазона модель
даже не сгенерирует. Грамматика -- первая линия защиты, а не
авторитет: сервер может проигнорировать её, и финальное слово
всегда за `tools.validate.parse_action`/`verify_action`.
"""

from __future__ import annotations

__all__ = ["build_action_grammar", "build_observation_grammar"]

# Порядок полей контракта фиксирован (ADR-0001 §2) -- не менять:
# repair-инструкции и парсер на него опираются.
_ACTION_ROOT = (
    'root ::= "{" ws "tool" ws ":" ws tool-name ws "," ws "args" ws ":" ws object ws '
    '"," ws "confidence" ws ":" ws confidence ws "," ws "abstain" ws ":" ws '
    '("true" | "false") ws "}" ws'
)

# Диапазон [0, 1]: "1" или "0" с дробью до 12 знаков. Дробь без целой
# части не разрешаем -- модель и так знает, как писать 0.9.
_CONFIDENCE_RULE = 'confidence ::= ("1" | "0" ("." [0-9]{1,12})?) ws'

# JSON-часть -- стандартная GBNF-грамматика JSON из примеров llama.cpp
# (grammars/json.gbnf), многолинейный формат принимают серверы;
# воспроизводить её иначе означало бы придумывать формат заново.
_JSON_RULES = r"""
value  ::= object | array | string | number | ("true" | "false" | "null") ws

object ::=
  "{" ws (
            string ":" ws value
    ("," ws string ":" ws value)*
  )? "}" ws

array  ::=
  "[" ws (
            value
    ("," ws value)*
  )? "]" ws

string ::=
  "\"" (
    [^"\\\x7F\x00-\x1F] |
    "\\" (["\\bfnrt] | "u" [0-9a-fA-F]{4})
  )* "\"" ws

number ::= ("-"? ([0-9] | [1-9] [0-9]{1,16})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,16})? ws

ws ::= | " " | "\n" [ \t]{0,20}
"""


def build_action_grammar(tool_names: list[str]) -> str:
    """Собрать GBNF для 4-полевого действия (контракт ADR-0001 §2).

    `tool_names` -- разрешённые в ТЕКУЩЕМ состоянии миссии инструменты
    (режим/гости/тур). Пустой список допустим: модель всё равно обязана
    ответить объектом, просто `tool-name` выродится в пустую альтернативу
    (на практике список не бывает пустым -- `reply` всегда разрешён).
    """
    name_rule = " | ".join(f'"\\"{name}\\""' for name in tool_names) or '""'
    return "\n".join(
        [
            _ACTION_ROOT,
            f"tool-name ::= {name_rule} ws",
            _CONFIDENCE_RULE,
            *_JSON_RULES.splitlines(),
        ]
    )


# Наблюдение (Taiga #4, observe_then_decide): компактный JSON -- люди,
# «видимые» экспонаты (id ТОЛЬКО из списка кандидатов), жест-указание,
# факты сцены. Ключевое отличие от action-грамматики: `exhibit_candidates`
# фиксируется на конкретные строки кандидатов (semantic map -- единственный
# источник id, инвариант issue #4), а не на общий `string`.
_OBSERVATION_ROOT = (
    'root ::= "{" ws "people_count" ws ":" ws people-count ws "," ws '
    '"exhibit_candidates" ws ":" ws candidate-array ws "," ws '
    '"pointing_evidence" ws ":" ws pointing ws "," ws '
    '"scene_facts" ws ":" ws string ws "}" ws'
)
_PEOPLE_COUNT_RULE = "people-count ::= [0-9]{1,2} ws"
_POINTING_RULE = 'pointing ::= ("none" | "yes" | "uncertain") ws'


def build_observation_grammar(candidate_ids: list[str]) -> str:
    """Собрать GBNF фазы наблюдения (Taiga #4, observe_then_decide).

    `candidate_ids` -- id кандидатов-экспонатов из семантической карты на
    МОМЕНТ ХОДА: модель не сгенерирует ни одного id вне этого списка
    (пустой список -- только пустой массив). `scene_facts` -- общий
    `string` (свободный текст, обрезается host-стороной в
    `visual_context.parse_observation`).
    """
    if candidate_ids:
        id_rule = " | ".join(f'"\\"{name}\\""' for name in candidate_ids)
        candidate_array = (
            'candidate-array ::= "[" ws (candidate-id ("," ws candidate-id)*)? "]" ws'
        )
        candidate_id_rule = f"candidate-id ::= ({id_rule}) ws"
    else:
        # Пустые кандидаты: единственный допустимый массив -- пустой;
        # правило candidate-id не объявляется вовсе (мёртвое правило с
        # пустой альтернативой не нужно).
        candidate_array = 'candidate-array ::= "[" ws "]" ws'
        candidate_id_rule = None
    rules = [_OBSERVATION_ROOT, _PEOPLE_COUNT_RULE, _POINTING_RULE, candidate_array]
    if candidate_id_rule is not None:
        rules.append(candidate_id_rule)
    return "\n".join([*rules, *_JSON_RULES.splitlines()])
