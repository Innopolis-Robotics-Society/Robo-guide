"""Системный промпт dialog_agent: преамбул (из файла) + каталог инструментов.

Чистая логика без rclpy -- каталог собирается из `tools/schema.py` (один
источник формулировок инструментов, не дублировать их текстом отдельно в
промпте). Преамбул НЕ хардкодится здесь -- он живёт в
`guide_robot_llm/config/system_prompt.txt`, читается `dialog_agent_node.py`
через параметр `system_prompt_path` и передаётся сюда текстом: тот же файл
(побайтово) должен греть `llm_server/config/system_prompt.txt` (SPEC
llm_server §6) -- если преамбул зашить в код, эти две копии неизбежно
разъедутся молча.

Системный промпт целиком -- статическая часть `messages`, обязана идти
ПЕРВОЙ и не меняться от хода к ходу: `CACHE_REUSE` на сервере переиспользует
префикс только если он побайтово совпадает с прошлым разом (llm_server SPEC
§2). Поэтому вызывающий (`dialog_agent_node.py`) обязан передавать сюда ВЕСЬ
каталог инструментов (`tools.schema.TOOLS`), а не отфильтрованный по текущему
состоянию: фильтрация -- дело `tools_allowed` в снимке (волатильная часть,
llm_plam.md §5), не системного промпта. Иначе префикс менялся бы при каждой
смене состояния тура, и кэш не работал бы вовсе.
"""

from __future__ import annotations

from collections.abc import Sequence

from guide_robot_llm.tools.schema import ToolSpec

__all__ = ["build_system_prompt"]


def build_system_prompt(preamble: str, tool_specs: Sequence[ToolSpec]) -> str:
    """Собрать системный промпт: `preamble` (из файла) + описание переданных инструментов."""
    catalog = "\n".join(f"- {spec.name}: {spec.description}" for spec in tool_specs)
    return f"{preamble}\n\nДоступные инструменты:\n{catalog}"
