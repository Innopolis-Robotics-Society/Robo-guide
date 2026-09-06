"""GBNF-грамматика tool-call JSON: по форме, не по содержимому (llm_plam.md §4).

`{"tool": "<один из переданных имён>", "args": <произвольный JSON-объект>}` --
семантику `args` (существует ли `location_id`, `outcome ∈ {0,1,2}` и т.п.)
по-прежнему проверяет `guide_robot_llm.tools.validate` в рантайме, здесь
только форма. Решение обсуждено явно: типизировать `args` под каждый
инструмент в GBNF означало бы держать грамматику в синхроне с сигнатурой
каждого инструмента отдельно от `tools/schema.py`/`tools/validate.py` --
источник дублирования без выигрыша там, где whitelist (например,
`location_id`) всё равно рантаймовый и в грамматику не укладывается.

Функция, не константа модуля: набор разрешённых инструментов -- это
`tools.schema.allowed_tools(mission_state)`, меняется с состоянием тура.

JSON-часть (`object`/`array`/`string`/`number`/литералы) -- стандартная
GBNF-грамматика JSON из примеров llama.cpp (`grammars/json.gbnf`), без
изменений по существу -- воспроизводить её иначе означало бы придумывать
формат заново без причины.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["build_tool_call_grammar"]

# Общая JSON-грамматика для `args` -- копия стандартной llama.cpp json.gbnf.
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


def _escape_tool_name(name: str) -> str:
    # Имена инструментов -- идентификаторы (`tools/schema.py`: latin+underscore),
    # но экранируем как строку по-честному, а не полагаемся на это как на инвариант.
    return name.replace("\\", "\\\\").replace('"', '\\"')


def build_tool_call_grammar(tool_names: Sequence[str]) -> str:
    """Собрать GBNF, фиксирующую форму `{"tool": <enum>, "args": <object>}`.

    `tool_names` -- обычно `tools.schema.allowed_tools(mission_state)`: пустой
    список -- вырожденный случай (в такой момент `dialog_agent` не должен
    вообще звать ЛЛМ с tool-grammar, но грамматика на пустом enum остаётся
    синтаксически валидной, просто ничего не сможет сгенерировать).
    """
    if not tool_names:
        tool_alt = '"\\u0000"'  # заведомо непроизносимая альтернатива, не пустая продукция
    else:
        tool_alt = " | ".join(f'"\\"{_escape_tool_name(name)}\\""' for name in tool_names)

    root = (
        'root ::= "{" ws "\\"tool\\"" ws ":" ws tool-name ws "," ws '
        '"\\"args\\"" ws ":" ws object ws "}" ws\n'
        f"tool-name ::= {tool_alt}\n"
    )
    return root + _JSON_RULES
