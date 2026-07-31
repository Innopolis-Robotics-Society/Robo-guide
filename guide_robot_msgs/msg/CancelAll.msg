# Широковещательная аварийная отмена речи.
#
# НЕ Empty: без epoch возникает гонка -- чанк, синтезированный за миллисекунды
# до отмены, доигрывается уже после неё. Получатель обязан отбрасывать всё,
# что помечено epoch строго меньше последнего полученного.

uint8 SCOPE_ALL=0
uint8 SCOPE_NARRATION=1
uint8 SCOPE_DIALOG=2
uint8 SCOPE_SAFETY=3

# Причины (для логов и разбора false-wake, не влияют на логику).
string REASON_BARGE_IN=barge_in
string REASON_WAKEWORD=wakeword
string REASON_NAV_EVENT=nav_event
string REASON_ESTOP=estop
string REASON_OPERATOR=operator

builtin_interfaces/Time stamp

# Монотонный счётчик. Инкрементируется отправителем на каждую отмену.
uint64 epoch

uint8 scope

string reason
