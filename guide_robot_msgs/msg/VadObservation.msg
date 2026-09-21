# Одно окно VAD, точно привязанное к диапазону capture-сэмплов.

std_msgs/Header header
string device_session_id

uint64 first_sample
uint32 sample_count

float32 probability
bool active
float32 state_duration
float32 level_dbfs

# True на первом окне после reconnect, rewind или пропуска capture-сэмплов.
# Подтверждение речи не разрешается переносить через такой разрыв.
bool discontinuity
