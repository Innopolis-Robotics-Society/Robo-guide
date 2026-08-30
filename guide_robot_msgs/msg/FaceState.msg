# Целевое выражение лица (CLAUDE_CODE_TASK_face_stage1.md §3.1).
#
# face_node отвергает неизвестные значения state (throttled warning,
# предыдущее выражение держится) -- в браузер не должна попадать
# невалидированная строка.

string state       # одно из: idle, listening, thinking, speaking, driving, sleep, error
float32 gaze_az     # градусы, -90..90, 0 = прямо вперёд
uint32 seq
