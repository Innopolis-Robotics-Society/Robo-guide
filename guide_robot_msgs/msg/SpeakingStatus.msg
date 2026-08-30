# Состояние голосового выхода.
#
# Не Bool: latched Bool при падении ноды навсегда застревает в true.
# Здесь есть stamp -- потребитель считает статус протухшим, если он старше
# двух периодов heartbeat (по умолчанию 200 мс -> протухание на 400 мс).

builtin_interfaces/Time stamp

bool speaking

# Текущий epoch выхода. Позволяет потребителю отличить "молчит, потому что
# закончил" от "молчит, потому что его только что отменили".
uint64 epoch

# Идентификатор активной цели Say, пустая строка если ничего не говорит.
string goal_id

uint8 priority
uint8 scope

# Оценка момента окончания текущей речи. Сумма уже синтезированного,
# но не проигранного, и оценки оставшегося синтеза. Не гарантия.
builtin_interfaces/Time expected_end

# stage4 §2.1: interruptible активной цели (Say.Goal.interruptible).
# false -- vad_node не публикует автоматический barge-in, пока этот статус
# свежий (см. staleness-проверку выше). Стоп-слово (wakeword_node) и
# e-stop этим полем не гейтятся -- они публикуют CancelAll независимо от
# vad_node, см. guide_robot_voice/lib/scheduler.py.
bool interruptible true
