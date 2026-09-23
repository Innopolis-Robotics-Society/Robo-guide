# Событие жизненного цикла входящей человеческой реплики.

uint8 EVENT_OPENED=0
uint8 EVENT_ENDPOINT=1
uint8 EVENT_FINAL=2
uint8 EVENT_DISCARDED=3

builtin_interfaces/Time stamp
string device_session_id
uint64 utterance_id
uint64 start_sample
uint64 end_sample
uint8 event
uint8 decision
string status
