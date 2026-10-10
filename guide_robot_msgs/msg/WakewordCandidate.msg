# Локальный KWS-кандидат, НЕ разрешение диалогу и НЕ команда отмены TTS.
# Принимается только voice_session_manager после проверки capture-сессии,
# свежести события и output/AEC guards.
std_msgs/Header header
string device_session_id
uint64 end_sample
string keyword
float32 confidence
string model_sha256
