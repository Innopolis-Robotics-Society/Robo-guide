# Фаза текущего хода dialog_agent (CLAUDE_CODE_TASK_face_stage2.md §1.4).
#
# Публикуется на /dialog/phase, TRANSIENT_LOCAL depth 1, ТОЛЬКО на смену
# (phase, presence) -- не по таймеру. Единственное новое поведение
# guide_robot_llm ради face_aggregator: сама логика хода (turn.py) этим
# сообщением не пользуется и о нём не знает.

uint8 IDLE=0
uint8 ACTION=1        # фаза 1 (выбор действия) в полёте
uint8 ANSWER=2        # фаза 2 (реплика) в полёте
uint8 AWAITING=3      # _pending_question жив, ждём ответа посетителя
uint8 phase

# Зеркалит тот же флаг, что dialog_agent уже использует для очистки
# истории (self._last_presence.present из /mission/presence) -- второе,
# независимое понятие присутствия здесь не считается.
bool  presence
