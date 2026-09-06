# Одна запись jsonl-лога dialog_agent (guide_robot_llm/llm_plam.md §6).
#
# JSON-строкой, не жёсткой схемой -- лог по природе схема-гибкий (новые
# поля не требуют пересборки сообщения), тот же приём, что args_json/
# data_json у CallTool.srv. Схема payload_json -- см.
# guide_robot_llm/guide_robot_llm/dialog/interaction_log.py:build_interaction_record.

string payload_json
