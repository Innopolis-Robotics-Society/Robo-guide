uint8 STATE_IDLE = 0
uint8 STATE_TOUR_ACTIVE = 1
uint8 STATE_NARRATING = 2
uint8 STATE_AWAITING_USER = 3
uint8 STATE_PAUSED = 4
uint8 STATE_ERROR = 5

uint8 state
string tour_id
uint32 stop_index
uint32 stop_count
string current_location
string current_zone
NarrationToken[] interrupt_stack
