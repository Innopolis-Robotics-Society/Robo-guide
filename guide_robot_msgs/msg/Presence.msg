# Присутствие посетителя, агрегированное presence_monitor из нескольких
# слабо связанных источников (wakeword, ASR-финалы, VAD, задел под
# /perception/people). design §6.
#
# present взводится немедленно любым свидетельством и снимается только
# после disengage_timeout_s без новых свидетельств -- поэтому present
# не эквивалентно "свидетельство только что было", для этого есть
# seconds_since_evidence.

std_msgs/Header header

bool present
builtin_interfaces/Time last_evidence
float32 seconds_since_evidence

# Источник последнего засчитанного свидетельства: "wakeword" | "asr_final"
# | "vad" | "people" | "mission_state".
string last_source
