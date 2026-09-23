# Решение voice_session_manager для одного входного речевого сегмента.

uint8 DECISION_ADMIT=0
uint8 DECISION_KWS_ONLY=1
uint8 DECISION_REJECT=2

builtin_interfaces/Time stamp
string device_session_id
uint64 utterance_id

# Акустическое начало речи в счётчике capture-сэмплов. ASR сам добавляет
# настроенный pre-roll перед этой точкой.
uint64 onset_sample

uint8 decision

# Монотонная версия решения: позднее решение с меньшим номером игнорируется.
uint64 control_sequence
string reason
