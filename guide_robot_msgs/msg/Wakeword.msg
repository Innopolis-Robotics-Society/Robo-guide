# Срабатывание детектора ключевого слова.
#
# Не String: без confidence и tts_active невозможно считать false-wake rate,
# который является приёмочной метрикой пакета.

std_msgs/Header header

string keyword
float32 confidence

# Говорил ли робот в момент срабатывания. Именно эти срабатывания образуют
# метрику false-wake-under-TTS и являются мерой качества AEC.
bool tts_active

# Азимут источника, рад, кадр mic_array. NaN если DoA недоступен.
float32 azimuth
