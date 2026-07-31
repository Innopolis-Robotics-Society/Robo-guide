# Результат VAD, покадрово.

std_msgs/Header header

bool active
float32 probability

# Длительность текущего непрерывного состояния, сек.
float32 state_duration

# RMS входного кадра, dBFS. Нужен для контроля шумового пола
# и для диагностики "микрофон не тот" / "AGC поехал".
float32 level_dbfs
