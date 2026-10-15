# Результат конкретного LLM-хода для bounded voice follow-up.
# Не выводится из перехода speaking true->false: TTS может ещё синтезировать.
builtin_interfaces/Time stamp
string device_session_id
uint64 utterance_id
uint64 start_sample
string dialog_session_id
uint64 turn_id
# true только при успешном, не прерванном и полностью озвученном ответе.
bool reply_completed
# true, если ответ ждёт реплики посетителя: ask_visitor или вопрос в конце.
# Только такой ход открывает окно без повторного имени.
bool expects_reply
