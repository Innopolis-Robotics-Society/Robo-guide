# Heartbeat разговорной FSM. Состояние относится к конкретной capture-сессии
# и версии перехода, поэтому поздний heartbeat не может откатить новую сессию.

uint8 STATE_INITIALIZING=0
uint8 STATE_LISTENING=1
uint8 STATE_ROBOT_SPEAKING=2
uint8 STATE_BARGE_PENDING=3
uint8 STATE_STOPPING_OUTPUT=4
uint8 STATE_USER_SPEAKING=5
uint8 STATE_RECOGNIZING=6
uint8 STATE_DEGRADED=7

builtin_interfaces/Time stamp
uint8 state
string device_session_id
uint64 utterance_id
uint64 transition_sequence
string reason
