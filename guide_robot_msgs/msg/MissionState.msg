# Снимок состояния mission_fsm (guide_robot_mission_design.md §2.1).
#
# Публикуется при каждом переходе, плюс безусловный heartbeat 1 Гц
# (design §7) -- на нём висит TopicRateWatchdog супервизора.

std_msgs/Header header

uint8 STATE_IDLE=0
uint8 STATE_GREETING=1
uint8 STATE_NAVIGATING=2
uint8 STATE_NARRATING=3
uint8 STATE_ANSWERING=4
uint8 STATE_AWAITING_CONFIRM=5
uint8 STATE_PAUSED=6
uint8 STATE_HELD=7
uint8 STATE_RETURNING=8
uint8 state

uint8 IRQ_NONE=0
uint8 IRQ_ANSWERING=1
uint8 IRQ_AWAITING_CONFIRM=2
uint8 interrupt              # верхушка стека прерываний
uint8 base_state             # состояние под прерыванием (== state, если IRQ_NONE)

uint8 PAUSE_NONE=0
uint8 PAUSE_USER=1
uint8 PAUSE_SAFETY=2
uint8 PAUSE_PRESENCE=3
uint8 pause_reason

string tour_id
uint16 stop_index
uint16 stop_total
string stop_id                # = Location.id / TourStop.location_id

string exhibit_id             # = TourStop.exhibit_id
uint16 chunk_index
uint16 chunk_total
bool   resume_available
string resume_token

bool   presence
uint32 epoch                  # последний известный epoch voice (CancelAll.epoch), для логов
string detail                 # человекочитаемая причина последнего перехода
