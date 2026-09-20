# Фактическое состояние PCM-выхода аппаратного audio owner.
#
# Это внутренний heartbeat-контракт. `tts_node` использует его вместо
# предположения, что успешно синтезированный PCM уже слышен из динамиков.

uint8 STATE_IDLE=0
uint8 STATE_BUFFERING=1
uint8 STATE_PLAYING=2
uint8 STATE_DRAINING=3
uint8 STATE_FENCED=4
uint8 STATE_ERROR=5

builtin_interfaces/Time stamp

string device_session_id
string goal_id
uint64 stream_id
uint64 generation

uint8 state

# Сэмплы mono PCM текущего stream. Один сэмпл затем дублируется во все
# физические playback-каналы XVF3800.
uint64 submitted_samples
uint64 presented_samples_estimate
uint32 buffered_samples

# Погрешность оценки presented_samples, обусловленная ALSA-буфером.
float32 uncertainty_ms

string reason
