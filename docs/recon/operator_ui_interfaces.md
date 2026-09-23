# Recon: интерфейсы для операторского UI (`guide_robot_operator_ui`)

Первичная разведка — 2026-09-08, на коммите `4f9b00a`. **Дельта-обновление —
2026-09-08, HEAD `2423411`** (диапазон `4f9b00a..2423411`, 115 файлов,
+4081/-1554). Режим: read-only разведка, правится только этот файл.
Цель — собрать точные факты (`package/path/file.py:LINE`) для проектирования веб-UI
(старт/стоп тура, езда на базу, сброс локализации, синхронный показ слайдов/видео).
Везде, где факт не подтверждён поиском по всему воркспейсу, стоит явная пометка
**НЕ НАЙДЕНО**. Мнения и предложенная архитектура — не даются, это запрещено ТЗ.

Секции, помеченные `> Обновлено под 2423411: ...`, были перепроверены и
исправлены/дополнены по факту диапазона `4f9b00a..2423411`. Остальные секции
диапазон не задел и не трогались.

---

## 1. Инвентарь `guide_robot_msgs`

Регистрация всех типов: `guide_robot_msgs/CMakeLists.txt:16-52` (`rosidl_generate_interfaces`).
Всего 30 интерфейсов: 4 action, 19 msg, 11 srv... (по факту в файле 15 msg верхнего
списка + доп. — см. таблицу ниже, itog 30 файлов, все зарегистрированы).

### 1.1 Actions (4)

| Интерфейс | CMake line | Goal / Result / Feedback | Server (кто) | Client (кто) |
|---|---|---|---|---|
| `AskUser.action` | `CMakeLists.txt:35` | Goal: `question, option_ids[], option_phrases[], timeout_s, speak_question, on_timeout(0=DEFAULT,1=ABORT,2=REPEAT_ONCE), default_answer` (`AskUser.action:7-16`). Result: `outcome(ANSWERED/TIMEOUT/CANCELED/PREEMPTED/REJECTED), answer, raw_text, confidence` (`:19-27`). Feedback: `stage(ASKING/LISTENING/REPEATING), remaining_s` (`:30-34`). | **НЕ НАЙДЕНО** — нет `ActionServer(...AskUser...)` нигде. | **НЕ НАЙДЕНО** — нет `ActionClient`. Только комментарии, что не реализовано: `guide_robot_mission_control/guide_robot_mission_control/cli.py:11-13` ("`AskUser`-сервер в `mission_fsm` не реализован, это осознанно отложено"), `interrupt_stack.py:25`, `fsm/states/answering.py:13`, `fsm/states/awaiting_confirm.py:3,14`. |
| `Narrate.action` | `CMakeLists.txt:33` | Goal: `exhibit_id, text, resume_token, priority, scope, continuity(CONTINUOUS/DROPPABLE)` (`Narrate.action:11-18`). Result: `outcome(COMPLETED/PAUSED/INTERRUPTED/ABORTED/REJECTED), resume_token, chunks_spoken, chunks_total, spoken_text, detail` (`:21-31`). Feedback: `chunk_index, chunk_total, chunk_text, progress` (`:34-37`). | `guide_robot_mission_control/guide_robot_mission_control/narration_server_node.py:201-206`, имя `"narrate"`. | `tool_broker_node.py:151-153`, `mission_fsm_node.py:183-185` (**без** `feedback_callback`, см. §3), `cli.py:233`. |
| `RunTour.action` | `CMakeLists.txt:34` | Goal: `tour_id, location_ids[], start_index, time_budget_min(не используется), greet, narrate, confirm_between_stops, return_home` (`RunTour.action:8-18`). Result: `outcome(COMPLETED/CANCELED/ABORTED/NO_VISITOR), stops_completed, stops_skipped, detail` (`:20-28`). Feedback: `phase(=MissionState.state), stop_index, stop_total, stop_id, eta_s(не используется)` (`:30-35`). Комментарий-владение: "Один активный goal. Второй — REJECT (не preempt)" (`RunTour.action:1-3`). | `mission_fsm_node.py:231-239`, имя `"run_tour"`. | `tool_broker_node.py:147-149` (tool `start_tour`/`guide_to`/`tour_by_points`), `cli.py:120`. |
| `Say.action` | `CMakeLists.txt:36` | Goal: `text, voice, priority(BACKGROUND=10/NARRATION=50/DIALOG=100/SAFETY=200), scope(ALL/NARRATION/DIALOG/SAFETY), interruptible, max_duration` (`Say.action:7-32`). Result: `status(COMPLETED/PREEMPTED/CANCELLED/REJECTED/FAILED), spoken_text, spoken_chars, spoken_duration, message` (`:34-52`). Feedback: `clause_index, clause_count, progress, current_clause` (`:54-61`). | `guide_robot_voice/guide_robot_voice/tts_node.py:170-176`, имя `"say"`. | `tool_broker_node.py:150`, `mission_fsm_node.py:186`, `narration_server_node.py:189,502-507` (**с** `feedback_callback`, потребляет `progress`), `cli.py:33`. **Обновлено под 2423411**: появился третий, C++-клиент — `guide_robot_bt_nodes/src/say_action.cpp:23-35` (`SayAction`, BT.CPP-нода `nav2_behavior_tree::BtActionNode<Say>`, action-имя `"say"`, `on_tick()` жёстко ставит `priority=PRIORITY_SAFETY(200)`, `scope=SCOPE_SAFETY(3)`, `interruptible=false` — `say_action.cpp:39-49`). Живёт внутри процесса `bt_navigator` (грузится как pluginlib `.so` `say_action_bt_node` через `nav2_tree_nodes.xml:1-10`), используется в recovery-ветках `guide_robot_navigation/behavior_trees/navigate_to_pose_with_recovery.xml:45,57` ("Внимание, отойдите!"/"Внимание, робот отъезжает!"), подключена через `default_nav_to_pose_bt_xml` в `guide_robot_navigation/config/first_iter_nav2.yaml.in:88` и `plugin_lib_names` (`:101-128`). См. §5.6. |

### 1.2 Messages (19)

| msg | Поля | Publisher | Subscriber |
|---|---|---|---|
| `AudioChunk.msg` (`:38`) | `header, sample_rate, channels, data[int16], first_sample` | `audio_frontend.py:180,183-185` (`/audio/mic`, `/audio/mic_raw`) | `vad_node.py:142-144`, `asr_node.py:214-216` |
| `CancelAll.msg` (`:39`) | `stamp, epoch, scope(ALL/NARRATION/DIALOG/SAFETY), reason(строковые константы BARGE_IN/WAKEWORD/NAV_EVENT/ESTOP/OPERATOR)` | `wakeword_node.py:104-106`, `vad_node.py:139-141`, `narration_server_node.py:191-193`, `cli.py:261` (`/speech/cancel_all`) | `tts_node.py:163-166`, `mission_fsm_node.py:202-205`, `narration_server_node.py:194-197`, `dialog_agent_node.py:338-341` |
| `ContentHit.msg` (`:46`) | `content_id, kind, title, chunk_id, text, score, version` | только вложено в `SearchContent.Response.hits`, конструируется `content_server.py:251` | нет отдельного топика — **НЕ НАЙДЕНО** |
| `DialogPhase.msg` (`:50`) | `phase(IDLE/ACTION/ANSWER/AWAITING), presence` | `dialog_agent_node.py:304-306` (`/dialog/phase`) | только тестовый: `test_dialog_phase_publisher.py:38`; production-подписчика ("face_aggregator", упомянут в комментарии `DialogPhase.msg:5-6`) — **НЕ НАЙДЕНО** |
| `Doa.msg` (`:40`) | `header, azimuth, beam_snr_db, voice_active` | **НЕ НАЙДЕНО** нигде вне `guide_robot_msgs` | **НЕ НАЙДЕНО** |
| `ExhibitChunk.msg` (`:47`) | `chunk_id, text, interruptible(default true), pause_after_s(default 0.0)` | только внутри `GetExhibitContent.Response.chunks` | — |
| `FaceState.msg` (`:48`) | `state, gaze_az, seq` | **НЕ НАЙДЕНО** ни одного publisher | `guide_robot_face/guide_robot_face/face_node.py:80` (`/face/state`) |
| `InteractionEvent.msg` (`:45`) | `payload_json` | `dialog_agent_node.py:299-301` (`/dialog/interaction`) | `interaction_log_node.py:54-57` |
| `Location.msg` (`:20`) | `id, aliases[], pose(geometry_msgs/PoseStamped), zone, category, is_public` | вложен в `ListLocations`/`ResolveLocation` responses | — |
| `MissionState.msg` (`:17`) | см. §3 (полная таблица) | `mission_fsm_node.py:227-229` (`/mission/state`) | `tool_broker_node.py:203-206`, `dialog_agent_node.py:310-313`, `presence_monitor_node.py:124-127`, `cli.py:176` |
| `Presence.msg` (`:44`) | `header, present, last_evidence, seconds_since_evidence, last_source` | `presence_monitor_node.py:129-131` (`/mission/presence`) | `tool_broker_node.py:210-213`, `dialog_agent_node.py:317-320` |
| `SonarRanges.msg` (`:37`) | `header, ranges[sensor_msgs/Range]` | **НЕ НАЙДЕНО** нигде, включая `guide_robot_sonar` | **НЕ НАЙДЕНО** |
| `SpeakingStatus.msg` (`:41`) | `stamp, speaking, epoch, goal_id, priority, scope, expected_end, interruptible` | `tts_node.py:156-158` (`/voice/speaking`) | `vad_node.py:145-147`, `wakeword_node.py:114-116`, `asr_node.py:218-220`, `mission_fsm_node.py:209-212`, `presence_monitor_node.py:119-122` |
| `SystemEvent.msg` (`:18`) | `severity(INFO/WARN/ERROR/CRITICAL), header, id, detail` | `audio_frontend.py:187-189`, `tts_node.py:160-162`, `content_server.py:84-86`, `location_server.py:118-120`, `route_planner.py:137-139` (`/system_event`) | **НЕ НАЙДЕНО** ни одного подписчика |
| `Tour.msg` (`:22`) | `id, name, duration_min_estimate, stops[TourStop], transit_content_id` | вложен в `ListTours.Response.tours` | — |
| `TourStop.msg` (`:21`) | `location_id, exhibit_id, dwell_s, mode` | вложен в `Tour.stops` | — |
| `Transcript.msg` (`:19`) | `header, utterance_id, text, is_final, confidence, speech_start, speech_end, language, azimuth` | `asr_node.py:207-212` (`/asr/partial`, `/asr/transcript`) | `wakeword_node.py:108-113`, `tool_broker_node.py:219-222`, `dialog_agent_node.py:324-327`, `presence_monitor_node.py:111-114` |
| `VoiceActivity.msg` (`:42`) | `header, active, probability, state_duration, level_dbfs` | `vad_node.py:137` (`/vad`) | `asr_node.py:217`, `presence_monitor_node.py:116-118` |
| `Wakeword.msg` (`:43`) | `header, keyword, confidence, tts_active, azimuth` | `wakeword_node.py:101-103` (`/speech/wakeword`) | `dialog_agent_node.py:331-334`, `presence_monitor_node.py:106-109` |

### 1.3 Services (11)

| srv | Request / Response | Server | Client |
|---|---|---|---|
| `CallTool.srv` (`:30`) | Req: `name, args_json, confirmed, mission_state_valid, mission_state` (`:10-27`). Resp: `ok, message, data_json` (`:29-31`) | `tool_broker_node.py:228-230` (`~/call_tool`) | `dialog_agent_node.py:293-294,1352` |
| `EstimateRoute.srv` (`:24`) | Req: `ids[], optimize`. Resp: `ordered_ids[], distance_m, duration_min, feasible` | `route_planner.py:140-144` (`~/estimate_route`) | `tool_broker_node.py:181-185,475-479` |
| `GetExhibitContent.srv` (`:25`) | Req: `exhibit_id, mode, language`. Resp: `chunks[ExhibitChunk], title, kind, version` | `content_server.py:87-89` (`~/get_exhibit_content`) | `tool_broker_node.py:191-195,681-706`, `mission_fsm_node.py:197-199,611`, `narration_server_node.py:187-189,400-423` |
| `ListLocations.srv` (`:23`) | Req: `zone, category, near_only`. Resp: `locations[Location]` | `location_server.py:121-123` (`~/list_locations`) | `tool_broker_node.py:173-177,633-643`, `mission_fsm_node.py:191-193` |
| **`ListTours.srv`** (`:27`) | Req: `language`. Resp: `tours[Tour]` — **это и есть интерфейс "список доступных туров"** | `location_server.py:127-129` (`~/list_tours`), читает `config/tours.yaml` через `lib/locations_io.py:254-264` | `tool_broker_node.py:178-180,647-660,708-729` (tool `list_tours`), `mission_fsm_node.py:190,577-593` (для разрешения `RunTour.Goal.tour_id`) |
| `NarrationControl.srv` (`:28`) | Req: `mode(SOFT/HARD), reason`. Resp: `ok, resume_token, chunks_spoken` | `narration_server_node.py:210` (`~/control`) | только тест `test_narration_resume.py:156` — production client **НЕ НАЙДЕНО** |
| `Redirect.srv` (`:32`) | Req: `location_id`. Resp: `accepted, message` | `mission_fsm_node.py:266-268` (`~/redirect`) | `tool_broker_node.py:168-170,462-466` |
| `ResolveLocation.srv` (`:26`) | Req: `query, language, max_results`. Resp: `candidates[Location], scores[], confident` | `location_server.py:124-126` (`~/resolve_location`) | `tool_broker_node.py:186-190` |
| `SearchContent.srv` (`:31`) | Req: `query, language, location_ids[], kinds[], max_results`. Resp: `hits[ContentHit]` | `content_server.py:90-92` (`~/search_content`) | `tool_broker_node.py:196-200,708-729` |
| `SetFaceState.srv` (`:49`) | Req: `state, gaze_az, seq`. Resp: `accepted, reason` | `face_node.py:81` (`/face/set_state`) | **НЕ НАЙДЕНО** ни одного client |
| `SubmitAnswer.srv` (`:29`) | Req: `outcome(RESUME_BASE/SKIP_STOP/END_TOUR)`. Resp: `accepted, message, resume_token` | `mission_fsm_node.py:260-264` (`~/submit_answer`) | `tool_broker_node.py:165-167` |

### 1.4 Явные проверки по ТЗ (стоп/пауза/докинг/initialpose)

| Понятие | Результат поиска |
|---|---|
| Старт тура | `RunTour.action` (см. выше) |
| Стоп тура | **Нет отдельного типа** `StopTour`/`CancelTour` — **НЕ НАЙДЕНО**. Реализовано как отмена goal-хендла `RunTour`: `tool_broker_node.py:514-520` (`_tool_stop_tour`, `goal_handle.cancel_goal_async()`), диспатч `tool_broker_node.py:769` |
| Пауза/резюм тура | **Нет** `PauseTour`/`ResumeTour` типа — **НЕ НАЙДЕНО**. Через generic `std_srvs/Trigger` сервисы `~/request_pause`/`~/request_resume`: `mission_fsm_node.py:245-253`, клиент `tool_broker_node.py:156-161` |
| Пауза/резюм именно нарратива | `NarrationControl.srv` — есть тип, но production-клиента нет (только тест) |
| Докинг/зарядка | Нет типа `Dock`/`DockRobot` — **НЕ НАЙДЕНО**. Есть только `bool return_home` в `RunTour.Goal` (`RunTour.action:18`) + состояние `MissionState.STATE_RETURNING=8` (см. §5) |
| `/initialpose` / сброс локализации | Нет кастомного типа `SetInitialPose`/`reset_localization` — **НЕ НАЙДЕНО**. `/initialpose` фигурирует только как топик инструмента RViz "2D Pose Estimate" (`guide_robot_bringup/rviz/sim.rviz:704`, `.../rviz/hardware.rviz:641`) — стандартный nav2-механизм, ни один узел репозитория его не публикует/не слушает |
| Прогресс озвучки/TTS | `Say.action` feedback `progress` (`Say.action:59`), потребляется `narration_server_node.py:502-507`; плюс `SpeakingStatus.msg` на `/voice/speaking` |

---

## 2. Управление экскурсией (`guide_robot_mission_control`)

### 2.1 Старт тура

Action `run_tour` (`guide_robot_msgs/action/RunTour.action`), сервер `mission_fsm_node.py:231-239`.

Обязательность полей **не декларативная** (у ROS `.action` нет required/optional), а
навязывается кодом:
- Единственный активный тур: второй `RunTour` во время активного отклоняется —
  `_on_run_tour_goal` проверяет `_active_ctx is not None` (`mission_fsm_node.py:415-423`).
- `tour_id` ИЛИ `location_ids` — по факту required: если `location_ids` пуст, код
  идёт в `ListTours` и ищет `tour_id`; если не находит — `OUTCOME_ABORTED,
  "tour_not_found"` (`mission_fsm_node.py:434-441`, `_resolve_tour` at `:566-593`).
- `time_budget_min` — объявлено в сообщении, но **нигде не читается** (`grep` по
  `mission_fsm_node.py` — 0 совпадений), мёртвое поле.
- `greet`/`narrate`/`confirm_between_stops`/`return_home` — обычные `bool`, без
  валидации; "дефолт true" у `greet` — это только соглашение клиента (`cli.py:128-131`
  явно ставит `greet=not args.no_greet`), а не серверная логика.

### 2.2 Стоп тура — "сейчас" vs "с подтверждением"

Два разных пути, оба реально в коде:

**A. Немедленный стоп = штатная ROS2 action-cancel на `run_tour`.**
`cancel_callback=lambda handle: CancelResponse.ACCEPT` (`mission_fsm_node.py:237`) —
**принимает cancel безусловно**, без проверки состояния/владельца. Проверяется на
каждом тике в базовом poll-цикле любого состояния FSM:
```
fsm/base.py:67-76  (_poll_loop): if self.ctx.is_cancel_requested(): cancel_active_work(...); return CANCELED
```
`is_cancel_requested()` читает `goal_handle.is_cancel_requested` напрямую
(`fsm/context.py:102-104`). `CANCELED` — терминальное состояние (не едет домой):
`fsm/root_sm.py:56-63,69` — комментарий подтверждает намеренность ("робот просто
стоит там, где был"). **Внешний клиент может обойти любое подтверждение**: у
`cancel_callback` нет проверки владельца/состояния — реальный пример такого
клиента уже есть, `cli.py:150-156` (`goal_handle.cancel_goal_async()` по Ctrl+C).

**B. "Стоп с подтверждением" = ветка `confirm_between_stops`/`AWAITING_CONFIRM`.**
Работает только если тур стартован с `confirm_between_stops=True`. Спрашивает
"Идём дальше?" (`fsm/states/awaiting_confirm.py:53-70`), ждёт ответ через сервис
`~/submit_confirm` (`std_srvs/SetBool`, `mission_fsm_node.py:254-259`, handler
`:753-761`) либо таймаут (`confirm_timeout_s`=20.0, `config/mission.yaml:15`). При
"нет"/таймауте — не мгновенная остановка, а переход в `"returning"` (едет домой)
(`fsm/root_sm.py:117-118`). Альтернативно — `~/submit_answer` (`SubmitAnswer.srv`)
с `OUTCOME_END_TOUR` тоже ведёт в `"returning"` (`fsm/root_sm.py:111`).

Итого: путь A — единственный способ остановить робота **на месте** немедленно,
путь B — способ **закончить тур с уходом домой**, оба доступны внешнему клиенту
через штатные ROS-интерфейсы (goal cancel / `~/submit_confirm` / `~/submit_answer`),
без специального разрешения/аутентификации.

### 2.3 Пауза/резюм

Есть. `PAUSED`/`RESUMED` — `fsm/states/paused.py:30-55`. Точки входа:
`~/request_pause`/`~/request_resume` (`std_srvs/Trigger`, `mission_fsm_node.py:245-253`,
handlers `:731-751`), CLI `mission_cli pause/resume` (`cli.py:190-217,293-298`).
Явный комментарий: это тестовый/ручной хук, не привязан к реальному presence —
`fsm/states/paused.py:1-18` ("в v1 вход/выход из PAUSED идёт через тестовые хуки...
TODO(stage2 A6)"). Enum `PAUSE_USER/PAUSE_SAFETY/PAUSE_PRESENCE` есть
(`MissionState.msg:25-29`), но реально выставляется только `PAUSE_USER`
(`mission_fsm_node.py:812-817`, `fsm/states/navigating.py:70`).

### 2.4 Список доступных туров

`guide_robot_mission_control` **не проксирует и не хранит** список туров сам —
он только клиент `ListTours` у `guide_robot_semantic_map`:
`self._list_tours_client = self.create_client(ListTours, "/location_server/list_tours")`
(`mission_fsm_node.py:190`). Сервер — `guide_robot_semantic_map/guide_robot_semantic_map/location_server.py:127-129`,
данные из `guide_robot_semantic_map/config/tours.yaml` (`location_server.py:109`,
`load_tours` в `lib/locations_io.py:254-264`). Значит: **операторский UI должен
звать `/location_server/list_tours` напрямую** (или через будущий прокси в
mission_control, которого сейчас нет) — читать `guide_robot_semantic_map` "напрямую
из файла" не нужно, штатный read-only сервис уже есть.

### 2.5 FSM-состояния тура

Wire-уровень (`guide_robot_msgs/msg/MissionState.msg:8-16`):
```
STATE_IDLE=0, STATE_GREETING=1, STATE_NAVIGATING=2, STATE_NARRATING=3,
STATE_ANSWERING=4, STATE_AWAITING_CONFIRM=5, STATE_PAUSED=6, STATE_HELD=7,
STATE_RETURNING=8
```
Маппинг Python-имя → enum: `mission_fsm_node.py:53-62`. Плюс внутреннее
Python-имя `"redirect_done"` без отдельного enum-значения (падает в `STATE_IDLE`
через `.get(name, MissionState.STATE_IDLE)`, `mission_fsm_node.py:789`;
определение состояния `fsm/states/redirect_done.py:23-26`, список всех имён
`fsm/root_sm.py:230-240`). Доп. под-enum'ы: `IRQ_NONE/IRQ_ANSWERING/
IRQ_AWAITING_CONFIRM` (`:19-21`, только `IRQ_ANSWERING` реально используется) и
`PAUSE_*` (см. §2.3).

### 2.6 Дельта 2423411: barge-in при навигации, поведение при cancel не изменилось

> Обновлено под 2423411: добавлено новое поведение при барж-ине во время
> транзитного нарратива; базовый poll-цикл/cancel-семантика перепроверены и
> не изменились.

- **`fsm/base.py` (poll-цикл cancel/safety_hold) не менялся в этом диапазоне**
  (`git diff 4f9b00a..2423411` для файла пуст). Структура прежняя:
  `_poll_loop` (`fsm/base.py:67-86`) сначала проверяет
  `deactivating_event`, затем `is_cancel_requested()` → `CANCELED`, затем
  `safety_hold_event` → `HELD`, затем redirect. Вывод §2.2 не изменился.
- **`fsm/states/navigating.py` (+69 строк)** — исправлен баг: раньше
  транзитный `Narrate`-goal (озвучка "по пути") запускался
  fire-and-forget и не отменялся при выходе из `NavigatingState`, из-за
  чего `narration_server`'s `_active_execution` оставался занят и
  `NarratingState` на прибытии получал `OUTCOME_REJECTED("busy")`,
  молча пропуская рассказ об остановке (баг воспроизведён вживую:
  "Q&A → lab_demo → тихий объезд точек", докстринг
  `navigating.py:24-32`). Добавлены `_bind_transit()`
  (`navigating.py:136-146`), `_stop_transit()` (`:148-173`, отменяет
  транзитный goal и ждёт до `hard_stop_result_timeout_s`) и новый
  `on_exit()` (`:208-211`), вызывающий `_stop_transit()`. Регресс-тест —
  `guide_robot_mission_control/test/test_tour_flow.py:491-536`
  (`test_transit_still_speaking_does_not_skip_stop`), проверяет
  `stops_skipped == 0`.
- **Новый исход `INTERRUPTED` для барж-ина в пути**: если барж-ин
  (`self.ctx.consume_barge_in()`) приходит во время `NavigatingState.poll`,
  активный `NavigateToPose` отменяется и FSM уходит в `"answering"`, а
  `RESUME_BASE` потом возвращает на ту же остановку —
  `navigating.py:86-90`, транзиция `root_sm.py:78-89`
  (`outcomes.INTERRUPTED: "answering"` внутри блока `"navigating"`).
  `CANCELED` по-прежнему терминален (`root_sm.py:69`, `_UNIVERSAL`),
  не ведёт в `"returning"` — вывод §2.2 не изменился.
- **`mission_fsm_node.py` (весь диф — один хук `_on_cancel_all`,
  `mission_fsm_node.py:402-411`) и `narration_server_node.py`
  (`narration_server_node.py:628-637`, тот же хук)**: барж-ин теперь
  триггерится не только `CancelAll.REASON_BARGE_IN`, но и
  `CancelAll.REASON_WAKEWORD` (было: только `REASON_BARGE_IN`). Никаких
  новых сервисов/топиков/экшенов у `mission_fsm_node.py` не появилось —
  полный список регистраций (`create_service`/`create_client`/
  `create_subscription`/`ActionServer`/`ActionClient`) на HEAD —
  `mission_fsm_node.py:183,186,187,190,191,198,202,209,216,219,231,245,248,254,260,266`
  — идентичен списку из первичной разведки.

---

## 3. Сигнал прогресса тура (критично для слайдов)

### 3.1 Топик `/mission/state`

- Тип: `guide_robot_msgs/msg/MissionState`.
- Publisher: `self._state_pub = self.create_lifecycle_publisher(MissionState, "/mission/state", QOS_MISSION_STATE)` — `mission_fsm_node.py:227-229`.
- QoS (`guide_robot_mission_control/guide_robot_mission_control/lib/qos.py:45-50`):
  ```python
  QOS_MISSION_STATE = QoSProfile(
      history=HistoryPolicy.KEEP_LAST, depth=1,
      reliability=ReliabilityPolicy.RELIABLE,
      durability=DurabilityPolicy.TRANSIENT_LOCAL,
  )
  ```
- Частота/триггер: **и событийно, и периодически**. Событийно — на каждом переходе
  FSM, `_on_fsm_state_changed` (`mission_fsm_node.py:786-825`, вызывается из
  `InterruptibleState.run()` — `fsm/base.py:62`). Периодически — безусловный
  heartbeat 1 Гц (`heartbeat_s`=1.0, `config/mission.yaml:22`), таймер
  `mission_fsm_node.py:303-305`, публикация `_publish_heartbeat` (`:838-857`); это
  подтверждено и комментарием в самом файле сообщения
  (`guide_robot_msgs/msg/MissionState.msg:3-4`).
- Гранулярность: **на уровне остановки/экспоната**, не чанка. Поля `stop_index`,
  `stop_total`, `stop_id`, `exhibit_id`, `next_stop_id`, `next_exhibit_id`
  реально заполняются (`mission_fsm_node.py:803-809`). Поля `chunk_index`/
  `chunk_total` **существуют в сообщении** (`MissionState.msg:44-45`), но
  **нигде не присваиваются** в `mission_fsm_node.py` (grep — 0 совпадений) —
  остаются на дефолте `0`.

> Обновлено под 2423411: перепроверено — `_on_fsm_state_changed`
> (`mission_fsm_node.py:786-836`) и `_publish_heartbeat` (`:838-856+`) на
> HEAD по-прежнему не трогают `chunk_index`/`chunk_total`; вывод не изменился.

### 3.2 Событие "начал/закончил говорить про экспонат X"

Отдельного топика **НЕ НАЙДЕНО**. `mission_fsm_node.py` только подписан на
`/voice/speaking` (`SpeakingStatus`, `mission_fsm_node.py:209-215`,
`presence_monitor_node.py:119-122`), сам ничего эквивалентного не публикует.
Единственное место, где "начал говорить" вообще детектируется — внутри
`narration_server_node.py`, по первому фидбеку `Say.action`:
`_on_say_feedback` (`narration_server_node.py:506-516`, комментарий `:10-14`
прямо говорит, что "started" выводится из первого feedback, а не публикуется
отдельно). Это **не топик**, а внутренний коллбек, наружу не транслируется.

`Narrate.action` feedback (`chunk_index, chunk_total, chunk_text, progress` —
`Narrate.action:34-37`) существует и это как раз пер-чанковый прогресс, но
**mission_fsm его даже не читает**: `NarratingState.on_enter` вызывает
`send_goal_async(goal)` **без** `feedback_callback`
(`fsm/states/narrating.py:45`) — то есть даже внутри mission_control этот
сигнал не потребляется, не то что не публикуется наружу.

**Вывод: сигнал о смене слайда может быть привязан только к моменту прибытия/
смены `stop_id`/`exhibit_id` в `/mission/state` (уровень экспоната), не к
конкретному предложению/фразе.**

> Обновлено под 2423411: `fsm/states/narrating.py` не входит в диапазон
> `4f9b00a..2423411` (diff пуст) — `send_goal_async(goal)` на
> `narrating.py:45` по-прежнему вызывается **без** `feedback_callback`,
> grep файла на `feedback_callback` — 0 совпадений. Вывод не изменился.

### 3.3 Идентификатор для маппинга слайда на шаг

Есть два уровня идентификаторов, оба реальные, из `/mission/state`:
`stop_id` (= `Location.id` / `TourStop.location_id`, комментарий
`MissionState.msg:34`) и `exhibit_id` (= `TourStop.exhibit_id`,
`MissionState.msg:36`). `stop_index`/`stop_total` — числовой индекс в
маршруте (`MissionState.msg:32-33`).

Реальные значения `exhibit_id` из `guide_robot_semantic_map/content/*.yaml`
(поле `exhibit_id:`, требуется парсером — `content_io.py:182`):
```
expo_city_model, expo_handoff, livox_mid70, innopolis_university, intro,
innopolis_city, promobot_m13_artist, transit_lab, expo_meeting, nav2_course,
sam3_autolabeling, robo_guide, lab_area
```
(файлы: `guide_robot_semantic_map/content/<exhibit_id>.ru.yaml`).

Реальные `id` (=`location_id`) из `guide_robot_semantic_map/config/locations.yaml`
(поле `id`, required — `lib/locations_io.py:155`), пример записи
(`locations.yaml:9-18`):
```yaml
- id: entrance
  graph_node: 1
  pose: {x: 4.5, y: 7.6, yaw: 0.0}
  zone: lab
  category: waypoint
  is_public: true
  exhibit_id: entrance
```
Другие реальные `id`: `robo_guide` (`:20`), `promobot_m13_artist` (`:31`),
`sam3_autolabeling` (`:42`), `nav2_course` (`:53`), `livox_mid70` (`:64`),
`expo_meeting` (`:81`), `expo_city_model` (`:93`), `expo_handoff` (`:105`).
Замечание: `locations.yaml`'s `exhibit_id` — обратная ссылка location→content
(поле опционально, `lib/locations_io.py:176`, `.get()` без required-проверки),
а `location_ids` внутри content-файла (`content_io.py:230`, тоже опционально) —
это прямая ссылка content→locations; это разные, хотя и пересекающиеся списки.

### 3.4 Прогресс TTS/сегмента озвучки

- `Say.action` feedback `progress` (float, `Say.action:54-61`) —
  **потребляется** только внутри `narration_server_node.py:502-507`
  (`feedback.feedback.progress`), это прогресс внутри одной фразы/клаузы TTS.
- `Narrate.action` feedback (`chunk_index/chunk_total/chunk_text/progress`,
  `Narrate.action:33-37`) — прогресс по чанкам нарратива, но, как указано в
  §3.2, **не читается mission_fsm** (нет `feedback_callback`), соответственно
  не может попасть в `/mission/state` в текущем коде.
- `Narrate.Result` содержит `chunks_spoken/chunks_total/spoken_text/resume_token`
  (`Narrate.action:26-31`) — видно только прямому клиенту `narrate`-action
  (mission_fsm или `mission_cli say`, `cli.py:231-256`) по завершении, не в
  реальном времени.
- `NarrationControl.srv` ответ содержит `chunks_spoken`/`resume_token`
  (`NarrationControl.srv:11-13`) — синхронный ответ на вызов `~/control`, не
  вещание.

**Вывод: наружу (в `/mission/state`, единственный публично видимый
широковещательный источник прогресса тура) доступна только гранулярность
экспоната/остановки. Пер-чанковый и пер-фразовый прогресс TTS существуют в
памяти узлов (`narration_server_node.py`, `tts_node.py`), но никуда не
транслируются за пределы прямого action-клиента конкретного goal.**

---

## 4. `guide_robot_semantic_map`

### 4.1 Схема контента

Файлы: `guide_robot_semantic_map/content/*.ru.yaml` (13 штук), формат YAML,
парсинг `yaml.safe_load` в `lib/content_io.py:309`. Соглашение об имени файла
`<exhibit_id>.<language>.yaml` (`lib/content_io.py:10-13`, проверка
`_check_filename_consistency`, `:274-285`).

Пример (`content/expo_city_model.ru.yaml:1-19`):
```yaml
exhibit_id: expo_city_model
language: ru
version: "2026-08-29.1"
reviewed_by: ""
title: "Макет Иннополиса"
chunks:
  - {id: c1, level: short, interruptible: true, text: "..."}
  - {id: c2, level: full, interruptible: true, text: "..."}
  - {id: c3, level: full, interruptible: false, pause_after_s: 1.0, text: "..."}
```

Required/optional по факту доступа в `lib/content_io.py`:

> Обновлено под 2423411: номера строк сдвинулись (файл вырос на +29 строк —
> добавились `normalize_exhibit_key`/`resolve_exhibit_id` перед парсером, см.
> ниже), сами поля схемы **не изменились**, новых полей не добавлено.

| Поле | Код | file:line (HEAD `2423411`) | Required? |
|---|---|---|---|
| `exhibit_id` | `_require_str(...)` | `:209` | **required** |
| `language` | `_require_str(...)` | `:210` | **required** |
| `version` | `_require_str(...)` | `:211` | **required** |
| `title` | `_require_str(...)` | `:212` | **required** |
| `reviewed_by` | `.get("reviewed_by")` | `:213` | optional |
| `reviewed_at` | `.get("reviewed_at")` | `:214` | optional |
| `kind` | `.get("kind", "exhibit")` | `:248` (`_parse_kind`) | optional, дефолт `"exhibit"` |
| `location_ids` | `.get("location_ids")` | `:257` (`_parse_location_ids`) | optional, дефолт `[exhibit_id]`/`[]` |
| `chunks` (список) | прямой `isinstance` без дефолта | `:218` | **required**, минимум 1 |
| `chunks[i].id` | `_require_str(...)` | `:268` | **required** |
| `chunks[i].level` | `.get("level")` + валидация без дефолта | `:269` | **фактически required** (`{"short","full"}`) |
| `chunks[i].text` | `_require_str(...)` | `:274` | **required** |
| `chunks[i].interruptible` | `.get("interruptible", True)` | `:275` | optional, дефолт `True` |
| `chunks[i].pause_after_s` | `.get("pause_after_s", 0.0)` | `:289` (`_parse_pause_after_s`) | optional, дефолт `0.0`, диапазон `[0,15]` |

Инварианты (минимум один чанк `level: short`, уникальность id чанков,
совпадение `exhibit_id`/`language` с именем файла) — сохранены, только
сдвинулись строки внутри `_parse_content`/`_check_filename_consistency`.

**Новое на HEAD**: две новые функции, не влияющие на схему YAML, а на
разрешение `exhibit_id` в запросах —
`normalize_exhibit_key` (`lib/content_io.py:101-103`) и
`resolve_exhibit_id` (`lib/content_io.py:106-123`), обе экспортированы в
`__all__` (`:31,33`). См. §4.4.

### 4.2 Поля под медиа

**НЕ НАЙДЕНО.** Ни в `content/*.yaml`, ни в `lib/content_io.py`, ни в
`ExhibitChunk.msg`/`ContentHit.msg` нет полей `image`/`video`/`slide`/`media`/
`duration`/`url`/`photo`/`thumbnail`/`audio`. Единственное "duration" в пакете
— это `estimate_duration_min`/`Tour.duration_min_estimate` (время прохождения
тура, `lib/estimate.py:43`), не длительность медиа-ролика.

> Обновлено под 2423411: перепроверено на HEAD — grep `content_io.py` и
> `content_server.py` (case-insensitive) на `media|image|video|slide|duration|url`
> по-прежнему 0 совпадений. Вывод не изменился.

### 4.3 Куда физически легли бы медиа-файлы (по текущим конвенциям)

Пакет `ament_python` (`package.xml:32`, `<build_type>ament_python</build_type>`),
данные ставятся через `setup.py`:
```python
# guide_robot_semantic_map/setup.py:13-19
data_files=[
    ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
    (f"share/{PACKAGE_NAME}", ["package.xml"]),
    (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
    (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml") + glob("config/*.geojson")),
    (f"share/{PACKAGE_NAME}/content", glob("content/*.yaml")),
],
```
Runtime resolve через `ament_index_python`:
`content_server.py:17` (импорт), `:75` (`get_package_share_directory('guide_robot_semantic_map')/content`),
`:124` (аналогично для `config/locations.yaml`). По этой же конвенции новая
папка (например `content/media/`) легла бы новой строкой в `data_files`
(по образцу `setup.py:18`) — **но такого пути сейчас нет**.

### 4.4 Read-only API

Оба сервиса регистрируются в `ContentServerNode._configure`
(`content_server.py:87-92`), нода `content_server` (`:38-43`):

- `~/get_exhibit_content` — `GetExhibitContent.srv` (req: `exhibit_id, mode,
  language`; resp: `chunks[ExhibitChunk], title, kind, version`). Обработчик
  `_on_get_exhibit_content` (`content_server.py:170-224`): выбор языка через
  `pick_language` (`:189`, `lib/content_io.py:161-172`), при языковом фолбэке
  публикует `SystemEvent` (`:207-209`).
- `~/search_content` — `SearchContent.srv` (req: `query, language,
  location_ids[], kinds[], max_results`; resp: `hits[ContentHit]`).
  Обработчик `_on_search_content` (`:226-262`), ограничение
  `max_results ≤ 20` (`_SEARCH_MAX_RESULTS`, `:34-35,241-242`), делегирование
  в `ContentIndex.search` (`lib/search.py`).

Клиенты в остальном репо: `guide_robot_llm/guide_robot_llm/tool_broker_node.py:191-198,681-729`
(`_tool_lookup_content`/`_tool_search_content`), `guide_robot_mission_control/guide_robot_mission_control/mission_fsm_node.py:199,611`,
`guide_robot_mission_control/guide_robot_mission_control/narration_server_node.py:187,400-423`.

> Обновлено под 2423411: `~/get_exhibit_content` подешевле на входе — только
> `~/get_exhibit_content` (`~/search_content` не тронут, `_on_search_content`
> не менялся). `_on_get_exhibit_content` теперь сначала прогоняет
> `request.exhibit_id` через `resolve_exhibit_id()`
> (импорт `content_server.py:27`, вызов `content_server.py:186`), которая
> возвращает `exhibit_id` не только по точному совпадению id, но и по
> точному совпадению нормализованного `title` контента
> (`lib/content_io.py:106-123`, нормализация `normalize_exhibit_key`,
> `:101-103`). Если совпадение произошло по title, а не по id, пишется
> info-лог `content_server.py:193-196`. Если совпадения нет вообще —
> warning и пустой ответ (`content_server.py:187-191`), поведение не
> изменилось. Регресс-тест: `guide_robot_semantic_map/test/test_content_io.py:424-434`
> (`test_resolve_exhibit_id_by_title`) — проверяет
> `resolve_exhibit_id("Знакомство и приглашение в Иннополис", catalog) == "expo_meeting"`
> и `resolve_exhibit_id("нет такого", catalog) is None`. Никакого нового
> ROS-сервиса не добавлено (`grep -n "create_service"` в
> `content_server.py` — по-прежнему ровно 2 регистрации).
>
> Также перепроверен `guide_robot_semantic_map/config/locations.yaml` (115
> строк, диф +4/-2): все 9 `id` из §3.3 — `entrance, robo_guide,
> promobot_m13_artist, sam3_autolabeling, nav2_course, livox_mid70,
> expo_meeting, expo_city_model, expo_handoff` — присутствуют без
> изменений; правка коснулась только алиасов записи `expo_meeting`
> (`locations.yaml:89-90`, добавлены `"знакомство с иннополисом"`,
> `"иннополис"`, `"анополис"` в `ru` и `"innopolis intro"` в `en`). Записи
> `home`/`base`/`dock` по-прежнему **НЕ НАЙДЕНО**. `package.xml` (+1
> строка) — не новая зависимость, а XML-комментарий
> (`package.xml:24`): `<!-- rank_bm25: нет apt/rosdep-ключа. В образе:
> .docker/common/91-python-apps.sh -->`.

---

## 5. Возврат на базу

### 5.1 Команда докинга/зарядки

**НЕ НАЙДЕНО** как отдельный интерфейс. Exhaustive grep по `dock|base|home|
charging|return` в `mission_control`, `navigation`, `bringup`, `supervisor`,
`msgs` — ничего, кроме RViz-панели ("Hide Left/Right Dock" — не связано с
роботом). Единственное, что есть — **булево поле `return_home` внутри
`RunTour.Goal`** (`RunTour.action:18`), обрабатываемое состоянием
`ReturningState` (`guide_robot_mission_control/guide_robot_mission_control/fsm/states/returning.py`),
которое просто шлёт обычный `NavigateToPose` на статичную точку. Никакой
докинг-специфичной логики (контакт зарядки и т.п.) нет.

### 5.2 Где хранится поза базы

Плоский ROS-параметр в `map`-фрейме, не TF-фрейм и не запись в semantic_map:
```yaml
# guide_robot_mission_control/config/mission.yaml:27-28
home_frame: "map"
home_pose: [0.0, 0.0, 0.0]   # x, y, yaw (yaw пока не используется)
```
Декларация параметров: `mission_fsm_node.py:127-128`. Сборка `PoseStamped`
(только x/y, ориентация не выставляется) — `mission_fsm_node.py:527-535`.
Использование — `fsm/states/returning.py:33-35`
(`goal.pose = self.ctx.home_pose(); self.ctx.nav_client.send_goal_async(goal)`).
В `guide_robot_semantic_map/config/locations.yaml` записи "home"/"base"/"dock"
**НЕ НАЙДЕНО**.

### 5.3 Навигация к произвольной точке

**Прямой `NavigateToPose` action-клиент**, никакой обёртки в
`guide_robot_navigation` для этого нет:
```python
# mission_fsm_node.py:187-189
self._nav_client = ActionClient(self, NavigateToPose, "navigate_to_pose", callback_group=self._cb_reentrant)
```
Точки отправки goal: `fsm/states/navigating.py:56-58` (остановка тура) и
`fsm/states/returning.py:33-35` (возврат домой). Оба берут позу либо из
разрешённого маршрута semantic_map, либо из `home_pose` — **произвольную
внешнюю позу через этот клиент не отправить**, нет соответствующего входа.

Отдельно есть `guide_robot_semantic_map/guide_robot_semantic_map/route_planner.py:49-50,102-104`
— клиент `nav2_msgs/action/ComputeRoute` (Route Server), но это **только оценка
маршрута/стоимости** для `~/estimate_route`, не движение.

### 5.4 Единственный легальный клиент Nav2

`mission_fsm_node.py` (пакет `guide_robot_mission_control`) — единственный
production-клиент `NavigateToPose` во всём репозитории (grep по
`NavigateToPose|ActionClient|nav2_msgs`, исключая build/install/log/тесты).
`guide_robot_supervisor/guide_robot_supervisor/supervisor_node.py:30,39`
использует только `nav2_msgs.srv.ManageLifecycleNodes` — это управление
жизненным циклом узлов, не движение.

### 5.5 Как supervisor относится к внешним командам движения

Supervisor **не перехватывает goal-запросы** — он гейтит только lifecycle
группы через `ManageLifecycleNodes` (`supervisor_node.py:107-117,176-184`).
Полное чтение `supervisor_node.py` (424 строки) подтверждает: нет action
client/server, нет подписки на что-либо, связанное с `navigate_to_pose`/
`cmd_vel`, нет кода, который инспектировал бы или отклонял `NavigateToPose`
запрос. Механизм — pause/resume целой lifecycle-группы `navigation` при срабатывании
watchdog'ов (`config/supervisor.yaml:66-70,81-85,92,97,109`, обработка в
`_apply_policies`/`_do_action`, `supervisor_node.py:279-330`). Если Nav2-ноды
станут неактивны — присланный извне `NavigateToPose` может провалиться на
стороне самого Nav2 (вне этого пакета), но supervisor его не отклоняет сам.

Реальный **гейт команд движения** — не в supervisor, а в самом
`guide_robot_mission_control`, и только для собственных голов FSM:
- Подписки `mission_fsm_node.py:216-225` на `/supervisor/estop` (`Bool`) и
  `/supervisor/state` (`String`).
- `_recompute_safety_hold()` (`:389-400`): `held = estop or state in
  ("FAULT","SHUTDOWN")`.
- Любое состояние FSM на каждом тике проверяет `safety_hold_event`
  (`fsm/base.py:67-76`) и форсированно уходит в `HELD`
  (`fsm/states/held.py:24-41`), отменяя активный `NavigateToPose`.

Это **кооперативный самогейт единственного клиента**, а не системный перехват:
гипотетический сторонний клиент, шлющий `NavigateToPose` напрямую в тот же
Nav2 action server, ничем в этом репозитории не заблокирован.

> Обновлено под 2423411: `supervisor_node.py` сам не менялся в этом
> диапазоне (только `config/supervisor.yaml`/`supervisor_slam.yaml`, см.
> §6.1) — вывод "supervisor не перехватывает `NavigateToPose`-goal'ы"
> остаётся верным. Но появился **новый, отдельный от supervisor'а
> механизм**, который на HEAD реально блокирует движение по `/supervisor/estop`
> на уровне cmd_vel (не goal-уровне): в `guide_robot_navigation/launch/common.launch.py:85-94`
> добавлена вторая `twist_mux`-нода `mux_final`, и её конфиг
> (`guide_robot_navigation/config/first_iter_nav2.yaml.in:532-547`)
> объявляет `locks: {e_stop: {topic: /supervisor/estop, timeout: 0.0,
> priority: 255}}` — то есть `/supervisor/estop=true` теперь жёстко
> запирает выход `mux_final` (а значит и
> `/diff_drive_controller/cmd_vel_unstamped`) независимо от того, что шлёт
> Nav2 или админ-телеop. Это не отменяет вывод "supervisor не гейтит
> `NavigateToPose`-запросы" (гейтится cmd_vel, не сам goal), но означает,
> что `/supervisor/estop` перестал быть сигналом, который слушает только
> `mission_fsm_node.py` — теперь у него есть второй, аппаратно-эффективный
> потребитель вне пакета `guide_robot_supervisor`. См. §6.1 для полной
> схемы нового motion-path (`mux_safety`→`collision_monitor`→`mux_final`).

### 5.6 Новое: `guide_robot_bt_nodes` — влияние на Nav2/Say, не на клиента NavigateToPose

> Обновлено под 2423411: появился новый пакет между `4f9b00a` и `2423411`.

Новый C++-пакет `guide_robot_bt_nodes` (`CMakeLists.txt`, `package.xml`,
`nav2_tree_nodes.xml`, `src/say_action.cpp`) добавляет BehaviorTree.CPP-ноду
`SayAction`, которая сама является `rclcpp_action`-клиентом
`guide_robot_msgs/action/Say` на имя `"say"`
(`say_action.cpp:23-35`, наследник `nav2_behavior_tree::BtActionNode<Say>`).
Она регистрируется как pluginlib-плагин `say_action_bt_node`
(`nav2_tree_nodes.xml:1-10`, `pluginlib_export_plugin_description_file`)
и грузится **внутри процесса `bt_navigator`**, а не как отдельная ROS-нода.

Используется в новом дереве восстановления
`guide_robot_navigation/behavior_trees/navigate_to_pose_with_recovery.xml`
(68 строк, root `NavigateToPose`, `RecoveryNode number_of_retries="6"`) —
дважды, в ветках `BackUpWithWarning` (`:45`, `<SayAction text="Внимание,
отойдите!" server_name="say"/>`) и `LargeBackupAndSpin` (`:57`, "Внимание,
робот отъезжает!"). Это дерево подключено как
`default_nav_to_pose_bt_xml` в
`guide_robot_navigation/config/first_iter_nav2.yaml.in:88`, а
`say_action_bt_node` — последний элемент `plugin_lib_names`
(`:101-128`). `guide_robot_navigation/package.xml` получил
`<exec_depend>guide_robot_bt_nodes</exec_depend>` и
`<exec_depend>twist_mux</exec_depend>` (`package.xml:17-18`).
`guide_robot_bt_nodes` нигде не упоминается напрямую в
`guide_robot_bringup/launch/*.py` — подключается только транзитивно через
`guide_robot_navigation`.

**Что это меняет для клиента `NavigateToPose` и отмены goal**:
единственный клиент `NavigateToPose` в системе — по-прежнему
`mission_fsm_node.py:187-189` (см. §5.4), `SayAction` им не является и не
конкурирует за владение `NavigateToPose` — это внутренний лист дерева,
которое исполняет сам `bt_navigator` в рамках уже принятого им
`NavigateToPose`-goal'а. `SayAction` не переопределяет `halt()`/
`on_cancelled()` (только `on_tick()`/`providedPorts()`,
`say_action.cpp:39-59`), поэтому использует стандартный `halt()` базового
класса `nav2_behavior_tree::BtActionNode<Say>` (`bt_action_node.hpp:307-332`
из системного пакета `nav2_behavior_tree`, не из этого репозитория) —
если `SayAction` в этот момент RUNNING, её `"say"`-goal явно отменяется
через `async_cancel_goal` при halt'е дерева, тем же механизмом, что и у
любой другой BT-ноды (`BackUp`, `Spin` и т.д.).

**Не определяется статическим чтением кода** (честно зафиксировано, а не
угадано): (а) действительно ли `bt_navigator`'s execute-callback вызывает
`haltTree()` синхронно в ответ на `cancel_goal_async()` от
`mission_fsm_node.py` — код `nav2_bt_navigator` не входит в этот
репозиторий и не читался; (б) есть ли у `tts_node.py`'s `"say"`-экшен-сервера
побочный эффект, переживающий отмену goal'а (например, уже поставленный в
аппаратный буфер звук) — файл `tts_node.py` не входил в задание этой
проверки. Оба пункта — открытые вопросы, не факты этого отчёта (см. §9).

Также важно для §1.1 (Say.action): теперь у `"say"` минимум **три**
клиента — `narration_server_node.py`, `tool_broker_node.py` (Python,
`rclpy`) и `say_action_bt_node`/`SayAction` (C++, внутри `bt_navigator`,
`priority=SAFETY=200, scope=SAFETY, interruptible=false` —
`say_action.cpp:39-49`, что численно выше `PRIORITY_NARRATION=50` в
`Say.action:14`), плюс сам `mission_fsm_node.py:186` независимо создаёт
свой собственный `Say`-клиент. Приоритет `SAFETY` означает, что
BT-озвучка манёвра теоретически может прервать активный нарратив —
это релевантно для операторского UI, если он рассчитывает на
предсказуемую последовательность озвучки во время тура.

---

## 6. Сброс локализации

> Обновлено под 2423411: `first_iter_nav2.yaml.in` переписан (-241 сеть),
> `generated_config/first_iter_nav2.yaml` удалён из git (был закоммиченным
> артефактом рендера, теперь генерируется только при сборке),
> `common.launch.py` дополнен новой цепочкой `twist_mux`. Ядро локализации
> (AMCL по умолчанию, отсутствие EKF, отсутствие runtime-сброса позы) —
> не изменилось.

### 6.1 Реально запускаемый стек

Оба входа (`hardware.launch.py`, `simulation.launch.py`) по-прежнему ветвятся
по аргументу `slam` (default `"false"`, не менялся — `hardware.launch.py:60-64`,
`simulation.launch.py:36-40`, `nav_stack.launch.py:48-52`; сам
`nav_stack.launch.py` в диапазон `4f9b00a..2423411` не попал):
- `slam:=false` (дефолт) → `guide_robot_navigation/launch/navigation.launch.py:41-51`
  включает `nav2_bringup`'s `localization_launch.py`, который поднимает
  `nav2_map_server`, **`nav2_amcl`** и `lifecycle_manager_localization`
  (`node_names: ['map_server','amcl']`). Параметры — `guide_robot_navigation/config/first_iter_nav2.yaml`
  (шаблон `.yaml.in`, рендерится `render_params.py` — механизм рендера не
  изменился, см. ниже).
- `slam:=true` → `guide_robot_navigation/launch/slam_navigation.launch.py:42-48`
  включает `slam_toolbox`'s `online_async_launch.py` (mapping-режим, не
  localization-режим — `mode:`-параметр под localization по-прежнему не
  найден).
- **robot_localization/EKF: по-прежнему НЕ НАЙДЕНО.** Свежий grep на HEAD по
  `initialpose|set_pose|robot_localization|ekf_node|ekf_filter|
  PoseWithCovarianceStamped` в `guide_robot_navigation`, `guide_robot_bringup`,
  `guide_robot_supervisor`, `guide_robot_mission_control` — 0 совпадений.
  Не задеплен, нет зависимости в `package.xml`.
- `guide_robot_bringup/launch/nav_stack.launch.py:92-120` — точка ветвления
  (файл не менялся в этом диапазоне).

**Расхождение с корневым CLAUDE.md другого проекта**: формулировка "AMCL
отключён, используется только static map→odom transform" относится к
СОСЕДНЕМУ проекту (`iros_llm_swarm`), а не к `Robo-guide`. Для `Robo-guide`
верно обратное — собственный `CLAUDE.md:126` этого репозитория (строка
сдвинулась с `:123` на `:126` в диапазоне `4f9b00a..2423411`) прямо говорит
про "AMCL's hardcoded `set_initial_pose`", что и подтверждается кодом: AMCL
запускается по умолчанию (`slam:=false` — дефолтная ветка).

**Новое: перестроен motion path ниже Nav2**, обновлённый корневой
`CLAUDE.md:73-79` этого репозитория теперь описывает:
```
Nav2 controller_server → /cmd_vel_nav → velocity_smoother → /cmd_vel
  → mux_safety (+ /safety_cmd_vel) → /cmd_vel_mux_safety
  → collision_monitor → /cmd_vel_filtered
  → mux_final (+ /admin_cmd_vel, lock /supervisor/estop)
  → /diff_drive_controller/cmd_vel_unstamped → ...
```
Реализовано в `guide_robot_navigation/launch/common.launch.py` (файл
переработан, +79/- строк): `nav2` core (`common.launch.py:37-45`,
`navigation_launch.py` из `nav2_bringup`, некомпозитно), затем
`collision_monitor` (`:49-55`) под своим `lifecycle_manager_safety`
(`:59-72`, как и раньше — переживает рестарт основного nav-стека), затем
**две новые `twist_mux`-ноды**: `mux_safety` (`:74-83`, вход `/cmd_vel`
приоритет 10 vs `/safety_cmd_vel` приоритет 20, выход
`/cmd_vel_mux_safety`) и `mux_final` (`:85-94`, вход
`collision_out=/cmd_vel_filtered` приоритет 10 vs `admin=/admin_cmd_vel`
приоритет 20, лок `e_stop=/supervisor/estop` приоритет 255, выход
`/diff_drive_controller/cmd_vel_unstamped`). Конфиг топиков/локов —
`guide_robot_navigation/config/first_iter_nav2.yaml.in:520-547`;
`collision_monitor`'s I/O topics — `:440-441` (`cmd_vel_in_topic:
"/cmd_vel_mux_safety"`, `cmd_vel_out_topic: "/cmd_vel_filtered"`). Новые
`exec_depend` в `guide_robot_navigation/package.xml:17-18`:
`twist_mux`, `guide_robot_bt_nodes`. Питающая этот путь новая
`guide_robot_bringup/launch/teleop.launch.py` (44 строки, новый файл)
запускает `teleop_twist_keyboard`, публикуя либо на `/safety_cmd_vel`
(дефолт, `full_control:=false`), либо на `/admin_cmd_vel`
(`full_control:=true` — по собственному описанию аргумента "admin bypass
(обходит e_stop и collision_monitor)", `teleop.launch.py:10-13`). Это
прямо релевантно для §5.5: `/supervisor/estop` теперь реально запирает
cmd_vel через `mux_final`'s lock (см. правку в §5.5).

### 6.2 Точный механизм установки позы

**НЕ НАЙДЕНО никакого runtime-механизма** — вывод не изменился, но значение
самой статичной позы изменилось. Свежий grep по `/initialpose|set_pose|
SetInitialPose|PoseWithCovarianceStamped|serialize|deserialize` в
`guide_robot_navigation`, `guide_robot_bringup`, `guide_robot_supervisor`,
`guide_robot_mission_control` на HEAD — по-прежнему 0 релевантных
совпадений.

Единственное, что "задаёт" начальную позу — статичный YAML-параметр AMCL,
**переписанный** в этом диапазоне (yaw изменился с `0.0` на `π`, и
появился явный комментарий про доковую позу):
```yaml
# guide_robot_navigation/config/first_iter_nav2.yaml.in:53-58
set_initial_pose: true
initial_pose:
  x: 0.0
  y: 0.0
  z: 0.0
  yaw: 3.141592653589793
```
(предшествующие пояснительные комментарии про позу дока и URDF-конвенцию
yaw=π — `first_iter_nav2.yaml.in:42-52`). Это первое явное текстовое
указание в коде на то, что `(0,0,π)` понимается именно как **поза
зарядного дока** — раньше это нигде прямо не формулировалось (см. §9,
"Снято дельтой"). Никакой ROS-ноды/сервиса, публикующей `/initialpose` или
вызывающей AMCL's `/set_pose`/`/reinitialize_global_localization` в этом
репозитории **не
существует**. RViz-топик `/initialpose` (`guide_robot_bringup/rviz/*.rviz`)
— это отдельный, никем в коде не обрабатываемый путь через стандартный
`nav2_amcl`, вне зоны ответственности guide_robot_* пакетов.

### 6.3 Узлы, которым нужен консистентный сброс состояния

| Узел/состояние | Reset-механизм в этом репо |
|---|---|
| AMCL particle filter | **НЕ НАЙДЕНО** прямого. Косвенно — `~/reset` супервизора делает полный `_shutdown_all()` (`SHUTDOWN` всей lifecycle-группы `localization` через `ManageLifecycleNodes`, затем повторный `INIT`) — `supervisor_node.py:360-367,255-261`. Это грубый рестарт узла целиком, не точечный сброс фильтра. |
| slam_toolbox pose graph | **НЕ НАЙДЕНО** — нет вызова `clear_pose_graph`/подобного сервиса нигде в репо |
| robot_localization EKF | **N/A** — узел не задеплоен (см. §6.1) |
| Одометрия (`diff_drive_controller`) | **НЕ НАЙДЕНО** — нет кода, вызывающего reset/`set_pose` на контроллере |
| Costmap'ы Nav2 | **НЕ НАЙДЕНО** — нет вызова `clear_entirely_*_costmap` нигде в репо |

### 6.4 Где хранится "стандартная поза"

Уже описано в §5.2/§6.2: `home_pose`/`home_frame` в
`guide_robot_mission_control/config/mission.yaml:27-28` — это цель
навигации для возврата, а не поза для инициализации локализации. Файл
**не менялся** в диапазоне `4f9b00a..2423411` (diff пуст), значение
по-прежнему `home_pose: [0.0, 0.0, 0.0]`. AMCL-инициализация — отдельный
статичный `initial_pose` в `guide_robot_navigation/config/first_iter_nav2.yaml.in:53-58`
(строка сдвинулась, значение yaw теперь `π`, см. §6.2). Это **два разных
значения в двух разных пакетах**: `home_pose` — по-прежнему `(0,0,0)`,
AMCL `initial_pose` — теперь `(0,0,π)`; концептуально не связаны кодом
(нет общей константы/импорта между `mission.yaml` и
`first_iter_nav2.yaml.in`), хотя оба явно подразумевают одну и ту же
физическую точку (доковую позу).

> Обновлено под 2423411: `guide_robot_semantic_map/config/locations.yaml`
> перепроверен полностью (115 строк) — записи `home`/`base`/`dock`
> по-прежнему **НЕ НАЙДЕНО**; единственное изменение файла — новые алиасы
> у `expo_meeting` (см. §4.4), не относящиеся к локализации/доку.

### 6.5 Что происходит с активным Nav2 goal при сбросе позы

Поскольку самого события "сброс позы" в коде не существует (§6.2), **нет и
кода отмены активного goal при этом событии** — необработано. Единственная
отмена goal в `guide_robot_mission_control` связана с паузой/таймаутом/
cancel/`HELD`, не с локализацией:
`fsm/states/navigating.py:66-72,77-80,144-148`, `fsm/states/returning.py:63-67`.
Если бы кто-то напрямую опубликовал `/initialpose` во время активного
`NavigateToPose`, ничто в `guide_robot_navigation`/`guide_robot_supervisor`/
`guide_robot_mission_control` на это не отреагирует — поведение целиком
определяется штатным (вне этого репозитория) Nav2/AMCL кодом.

> Обновлено под 2423411: перепроверено — вывод не изменился, но строки
> сдвинулись из-за правок в §2.6 (транзитный `Narrate`). Актуальные
> цитаты на HEAD: пауза/hold_position — `navigating.py:79-85`, барж-ин —
> `:86-90`, `nav_stop_timeout_s` — `:96-99`, `cancel_active_work` —
> `:202-206` (теперь явно не трогает транзитный `Narrate` — это делает
> отдельный `on_exit()`/`_stop_transit()`, `:208-211`, добавленный в этом
> диапазоне, см. §2.6). Ни один из этих путей по-прежнему не триггерится
> событием локализации.

---

## 7. `guide_robot_face` — что переиспользуется

> Обновлено под 2423411: появился новый узел-агрегатор (`aggregator_node.py`
> + `lib/aggregator.py` + `lib/qos.py`) и kiosk-скрипты (см. §8.5). §7.3
> ("resolver/агрегатор НЕ НАЙДЕНО") полностью устарело — переписано ниже.
> §7.1/7.2/7.5 перепроверены точечно: порт и WS-протокол не изменились по
> сути, `face_server.py` вообще не менялся (diff пуст). §7.6/7.7
> переписаны с учётом того, что в пакете теперь два rclpy-узла.

### 7.1 Сервер

- Библиотека: `aiohttp` (`face_server.py:13`, `from aiohttp import WSMsgType, web`).
  **Версия не запинена нигде** — `package.xml:13` объявляет unversioned rosdep-ключ
  `python3-aiohttp`; ни в `setup.py`, ни в Dockerfile'ах версии нет
  (**НЕ НАЙДЕНО** пина). Не изменилось.
- Порт: ROS-параметр `http_port`, дефолт `8090` в коде
  (`face_node.py:39`, `declare_parameter("http_port", 8090)` — номер строки
  сдвинулся с `:49` на `:39`, поскольку инлайновое определение
  `QOS_FACE_STATE` убрано из `face_node.py` в пользу импорта из
  `lib/qos.py:22`), значение переопределено в
  `guide_robot_face/config/face_node.yaml:5` (`http_port: 8090`, было
  `:8` — файл стал короче). **Значение порта не изменилось**, но
  комментарий над ним поменял тон: было "конфликт с сервисами на Jetson
  не подтверждён", стало `config/face_node.yaml:1`: `# Параметры
  face_node. Порт 8090 на Jetson (10.100.20.169) свободен.` — то есть
  риск конфликта теперь заявлен как снятый (в виде комментария, не кода
  — независимой проверки в репозитории для этого нет).
- Структура файлов: `face_server.py` (aiohttp `Application`, **без** rclpy,
  явно в docstring `face_server.py:1`, **не менялся** в этом диапазоне —
  `git diff` пуст) / `face_node.py` (rclpy `Node`, держит event loop в
  отдельном треде) / **новый** `aggregator_node.py` (rclpy `Node`, см.
  §7.3) / `web/index.html`,`face.js`,`face.css`, **новый** `web/states.json`
  (статика) / `config/face_node.yaml`, `config/face_states.yaml`.

### 7.2 WS-протокол

Однонаправленный, сервер → клиент — **не изменилось**. Хэндлер
`face_server.py:70-85` (маршрут `/ws`, регистрация `:29`) не тронут
диапазоном. Формат кадра (`face_server.py:49-51`):
```python
frame = {"state": state, "gaze_az": gaze_az, "seq": seq}
```
отправляется через `ws.send_json(frame)` (`:56`), при подключении сразу шлётся
последний известный кадр (`:78-79`). **Клиент серверу ничего не шлёт**
(кроме дефолтного WS ping/pong, коммент `face_server.py:75`) — подтверждено
свежим grep `web/face.js` на `ws.send` (0 совпадений).

`guide_robot_msgs/msg/FaceState.msg` не менял поля (`state: string,
gaze_az: float32, seq: uint32` — байт-в-байт как раньше), изменился только
комментарий на `FaceState.msg:7`: список допустимых значений `state`
вырос с `idle, listening, thinking, speaking, driving, sleep, error` до
добавления `happy, surprised, curious, sad, focused, shy` (те же 6 новых
состояний появились в `web/states.json:79-133` и `config/face_states.yaml`).
Схема сообщения/кадра не изменилась, только словарь допустимых `state`.

### 7.3 Resolver/агрегатор

> Обновлено под 2423411: полностью переписано — старый вывод "НЕ НАЙДЕНО"
> устарел, агрегатор реализован.

Появились `guide_robot_face/guide_robot_face/aggregator_node.py` (новый
rclpy-узел `face_aggregator`) и чистый от rclpy модуль
`guide_robot_face/guide_robot_face/lib/aggregator.py` (только `from
__future__ import annotations` и `dataclasses` — grep на `rclpy` в файле
даёт 0 совпадений). Плюс `lib/qos.py` (новый, единственный модуль в
`lib/`, которому по докстрингу разрешено импортировать `rclpy.qos`).

**Подписки агрегатора** (`aggregator_node.py:__init__`):
- `/supervisor/state` (`std_msgs/String`, depth 10) — `:59`
- `/voice/speaking` (`SpeakingStatus`, `QOS_VOICE_SPEAKING`) — `:60-62`
- `/dialog/phase` (`DialogPhase`, `QOS_DIALOG_PHASE`) — `:63`
- `/mission/state` (`MissionState`, `QOS_MISSION_STATE`) — `:64-66`
- `/mission/presence` (`Presence`, `QOS_MISSION_PRESENCE`) — `:67-69`
- `/vad` (`VoiceActivity`, `QOS_VAD`) — `:70`
- `/speech/wakeword` (`Wakeword`, `QOS_WAKEWORD`) — `:71`
- плюс таймер 0.2с (`:72`) для переоценки устаревания (тайм-аут речи,
  затухание wakeword-hold).

**Публикация**: `/face/state` (`FaceState`, `QOS_FACE_STATE`) — `:58`,
вызов `self._pub.publish(FaceState(state=state, gaze_az=0.0, seq=self._seq))`
(`:134`) — `gaze_az` у агрегатора всегда `0.0`, меняются только `state`/`seq`.
Ни `create_service`, ни `create_client` в агрегаторе нет.

**Приоритетная схема** (`lib/aggregator.py:21,37-55`), дословно:
```python
_PRIORITY = ("error", "driving", "speaking", "thinking", "listening")

def decide(inp: FaceInputs) -> str:
    listening = inp.vad_active or inp.wakeword_hold or inp.dialog_phase == PHASE_AWAITING
    flags = {
        "error": inp.supervisor_fault,
        "speaking": inp.speaking,
        "thinking": inp.dialog_phase in (PHASE_ACTION, PHASE_ANSWER),
        "driving": inp.navigating,
        "listening": listening,
    }
    for name in _PRIORITY:
        if flags[name]:
            return name
    return "idle" if inp.presence else "sleep"
```
т.е. жёсткий приоритет `error > driving > speaking > thinking > listening >
idle|sleep`; докстринг (`lib/aggregator.py:40`) поясняет, что "езда" намеренно
бьёт "думаю"/"говорю", чтобы старт тура и транзитный TTS не держали лицо
в "думаю"/"говорю" всю дорогу до остановки. Тестируется в
`guide_robot_face/test/test_aggregator.py:21-40` (`test_priority_order`)
и смежных тестах (`:13-56`) — полная приоритетная матрица покрыта, но
**сам класс `FaceAggregatorNode` (rclpy-часть) тестами не покрыт** — только
чистая функция `decide()` (grep `test/` пакета: нет импорта
`aggregator_node`, НЕ НАЙДЕНО).

**QoS-профили** (`lib/qos.py`, все `rclpy.qos.QoSProfile`, KEEP_LAST/depth 1):
`QOS_FACE_STATE`/`QOS_DIALOG_PHASE`/`QOS_MISSION_STATE`/
`QOS_MISSION_PRESENCE`/`QOS_VOICE_SPEAKING` — RELIABLE+TRANSIENT_LOCAL
(`qos.py:24-57`); `QOS_VAD` — BEST_EFFORT+VOLATILE (`:59-64`); `QOS_WAKEWORD`
— RELIABLE+VOLATILE (`:66-71`).

Аггрегатор **не вызывает** `SetFaceState` — это явно задокументировано в
его собственном докстринге, `aggregator_node.py:1-4`: "Не знает про
LLM-affect и не вызывает /face/set_state: face_node сам читает
/face/state." Планируемый в старом комментарии `SetFaceState.srv` "агрегатор
на stage 2" — теперь реализован именно так: через топик `/face/state`,
не через сервис. Клиента `SetFaceState` по-прежнему **НЕ НАЙДЕНО** нигде
в репозитории (grep всего workspace — только сервер и документация).

### 7.4 Установка статики и резолв путей

`ament_python`, без `CMakeLists.txt`. Установка:
```python
# guide_robot_face/setup.py:18
(f"share/{PACKAGE_NAME}/web", glob("web/*")),
```
Резолв в рантайме — `ament_index_python.packages.get_package_share_directory`:
`face_node.py:18` (импорт), `:47` (`share = Path(get_package_share_directory("guide_robot_face"))`),
`:50` (`web_root` дефолт `share/web`). Передаётся в `FaceServer(web_root=...)`
(`:70`), используется в `face_server.py:30` (`add_static("/", self._web_root)`)
и `:64` (`FileResponse(self._web_root / "index.html")`).

> Обновлено под 2423411: новый файл `web/states.json` (134 строки) — не
> WS-сообщение, а статика: отдаётся отдельным GET-роутом
> `face_server.py:28` (`/states.json` → `_handle_states`, `:66-68`,
> возвращает распарсенный `face_states.yaml`), клиент подгружает его при
> старте (`web/face.js:109-124`, `loadStates`). Устанавливается той же
> строкой `setup.py:18` (`glob("web/*")`), новый путь отдельно
> прописывать не пришлось.

### 7.5 Жизненный цикл соединений

Множество клиентов в `set[web.WebSocketResponse]`
(`face_server.py:23`, добавление `:73`, удаление в `finally` `:84`, плюс
проактивная очистка при ошибке отправки `:53-60`). Переподключение: сервер
хранит `_last_frame` и реплеит новому клиенту (`:24,52,78-79`); клиентский JS
делает экспоненциальный backoff-реконнект (`web/face.js:103-127`,
1000–10000мс).

> Обновлено под 2423411: перепроверено — `face_server.py` не входит в
> диапазон изменений (`git diff` пуст), вывод не изменился.

### 7.6 ROS-зависимости (переиспользуемость)

> Обновлено под 2423411: в пакете теперь **два** rclpy-узла, не один.

- `face_node.py`: `rclpy` (`:16`), `rclpy.node.Node` (`:19`), `QOS_FACE_STATE`
  теперь **импортируется** из `lib/qos.py:22` (раньше был определён инлайн
  в этом файле), подписка на `FaceState`/`/face/state` (`:70`), сервис
  `SetFaceState`/`/face/set_state` (`:71`). Нет `create_publisher`/
  `create_client` в этом файле.
- **Новый** `aggregator_node.py`: `rclpy` (`:12-13`), полноценный
  `rclpy.node.Node` со своими подписками/публикацией/таймером — полный
  список в §7.3. Нет `create_service`/`create_client` в этом файле.
- `lib/qos.py` импортирует `rclpy.qos` (`:10`) — по докстрингу
  (`:1-6`) единственный модуль в `lib/`, кому это разрешено; `lib/aggregator.py`
  подтверждённо свободен от rclpy (см. §7.3).
- `face_server.py` — по-прежнему чистый aiohttp-класс без ROS-импортов
  вообще, не менялся, теоретически переиспользуемый как библиотека.

Итог: `face_node.py` по-прежнему единолично держит HTTP/WS-поверхность и
единственный сервис `SetFaceState`; `aggregator_node.py` — единственный
publisher `/face/state` в графе запуска (`guide_robot_face/launch/face.launch.py`
теперь запускает оба узла, `face_node` и `face_aggregator`).

### 7.7 Отдельный пакет vs второй endpoint — факты (без рекомендации)

> Обновлено под 2423411: появление `aggregator_node.py` — уже готовый
> прецедент "второй rclpy-узел в том же пакете, что и HTTP-сервер", это
> меняет исходные факты за/против.

**За общий процесс/endpoint:** `face_server.py` по-прежнему
ROS-агностичный aiohttp `Application`-класс, принимающий `web_root`/`states`
параметрами конструктора — механически доступен для импорта из другого
процесса. `face_node.py` содержит паттерн "rclpy Node с фоновым
asyncio-сервером в отдельном треде" (`_run_server`, `face_node.py:85-91`,
мост `asyncio.run_coroutine_threadsafe`), который можно было бы
скопировать 1:1. **Новый факт**: пакет уже показал, что можно добавить
*второй независимый rclpy-узел* (`aggregator_node.py`) в тот же пакет,
подписанный на совершенно другой набор топиков (`/mission/state`,
`/dialog/phase`, `/mission/presence`, `/vad`, `/speech/wakeword`,
`/supervisor/state`), без переписывания `face_node.py`/`face_server.py` —
т.е. прецедент "разные rclpy-узлы, один пакет, общий launch-файл" уже
существует и работает (оба узла запускаются вместе из
`guide_robot_face/launch/face.launch.py`).

**За отдельный пакет/процесс:** `face_node.py:39`/`config/face_node.yaml:5`
по-прежнему держат порт 8090; комментарий теперь заявляет его свободным на
конкретном Jetson (`10.100.20.169`), но это утверждение в комментарии, не
гарантия кода — независимой проверки в репозитории для этого нет. Второй
эндпойнт в том же HTTP-процессе (не путать с "тем же пакете/launch") всё
ещё означал бы разделяемый event loop и общий crash domain между "лицом"
робота и операторским UI. Зависимости пакета выросли на одну строку —
`package.xml:10-14` теперь `rclpy, ament_index_python, guide_robot_msgs,
std_msgs (новое — нужен агрегатору для /supervisor/state),
python3-aiohttp` — но
по-прежнему не включают ничего специфичного для tour/nav/localization
(`RunTour`, `NavigateToPose` и т.д.) — они концептуально чужие роли
"лица"/аудио-I/O пакета (см. декларацию роли `guide_robot_voice` в
корневом CLAUDE.md), даже при наличии прецедента с несколькими узлами в
одном пакете.

---

## 8. Инфраструктура

> Обновлено под 2423411: `hardware.launch.py` (+46), `high_level_stack.launch.py`
> (+23), `simulation.launch.py` (+6), новый `teleop.launch.py` (+44),
> `package.xml` (+2). `nav_stack.launch.py`/`perception.launch.py`/
> `lidars.launch.py`/`desk.launch.py`/`view_robot.launch.py` — не менялись
> (отсутствуют в `git diff --stat`).

### 8.1 `guide_robot_bringup` — launch-файлы

9 файлов на HEAD (README пакета по-прежнему говорит "семь",
`guide_robot_bringup/README.md:17` — недосчёт не исправлен и стал ещё
больше не совпадать с реальностью, см. ниже):

| Файл | Роль | Менялся в 4f9b00a..2423411? |
|---|---|---|
| `launch/hardware.launch.py` | Реальный робот: `robot_state_publisher`+`ros2_control_node`+спавнеры контроллеров, включает `perception`/`nav_stack`/`high_level_stack`/**новый `llm_stack`** | **Да** (+46) |
| `launch/simulation.launch.py` | Gazebo: включает `guide_robot_simulation/launch/gazebo.launch.py` (`:81-86`) + те же стеки | Да (+6, косметика) |
| `launch/nav_stack.launch.py` | Точка ветвления SLAM/AMCL + `guide_robot_supervisor` | Нет |
| `launch/high_level_stack.launch.py` | `guide_robot_voice`+`guide_robot_semantic_map`+`guide_robot_mission_control`+**новая `guide_robot_face` GroupAction** | **Да** (+23) |
| `launch/perception.launch.py` | Реальные лидары или Gazebo-топики + опциональный сонар | Нет |
| `launch/lidars.launch.py` | 2× `sllidar_ros2` → `laser_sector_blanker`×2 → `dual_laser_merger` | Нет |
| `launch/desk.launch.py` | Voice + high_level + `guide_robot_llm`, без моторов/Nav2 | Нет |
| `launch/view_robot.launch.py` | Только URDF/TF, без `ros2_control` | Нет |
| `launch/teleop.launch.py` | **Новый файл.** `teleop_twist_keyboard`, ремап `cmd_vel`→`/safety_cmd_vel` (дефолт) или →`/admin_cmd_vel` при `full_control:=true` ("admin bypass (обходит e_stop и collision_monitor)", `teleop.launch.py:10-13`) | **Новый** (+44) |

**`hardware.launch.py` изменения**: новые аргументы `launch_face` (дефолт
`"true"`, `:95-99`) и `launch_llm` (дефолт `"true"`, `:100-105`); новая
`llm_stack` `GroupAction` (`:232-244`), включающая
`guide_robot_llm/launch/llm.launch.py` с `autostart: "false"` (супервизор
поднимает LLM сам после `semantic_map`, см. §5.5/§6.1 про новую группу
`llm` в `supervisor.yaml`); `high_level_stack`-включение теперь также
передаёт `"launch_face": launch_face` (`:224`). **Никакой mux/twist_mux
ноды в `hardware.launch.py` нет** — вся цепочка `mux_safety`/`mux_final`
живёт в `guide_robot_navigation/launch/common.launch.py` (см. §6.1), сюда
подключается только транзитивно через `nav_stack.launch.py` (сам не
менялся).

**`high_level_stack.launch.py` изменения**: новый аргумент `launch_face`
(`:77-79`), новая `face` `GroupAction` (`:161-168`), включающая
`guide_robot_face/launch/face.launch.py`.

**`package.xml` (+2 строки)**: новые `<exec_depend>guide_robot_face</exec_depend>`
и `<exec_depend>guide_robot_llm</exec_depend>` (`package.xml:36-37`).

**Полный текущий список `DeclareLaunchArgument` в `hardware.launch.py`**
(рефреш): `use_sim_time`(`:43-45`), `use_mock_hardware`(`:46-48`),
`launch_sensors`(`:50-52`), `launch_sonar`(`:53-55`), `nav`(`:57-59`),
`slam`(`:60-64`), `map`(`:65-69`), `nav_params_file`(`:70-74`),
`slam_params_file`(`:75-79`), `autostart_nav`(`:80-82`),
`autostart_supervisor`(`:83-88`), `launch_high_level`(`:90-94`),
**`launch_face`(`:95-99`, новое)**, **`launch_llm`(`:100-105`, новое)**,
`voice_params_file`(`:106-110`), `launch_foxglove`(`:112-114`),
`launch_rviz`(`:115-119`).

### 8.2 Механизм подключения

Оба паттерна: `IncludeLaunchDescription`+`PythonLaunchDescriptionSource`
(`hardware.launch.py:218-227,230-243,246-255`) для стеков и прямой `Node(...)`
для листовых драйверов (`hardware.launch.py:141-151,153-161,274-282`).
`GroupAction(scoped=True)` вокруг include'ов в `high_level_stack.launch.py:98-111,116-130,135-149`
— для изоляции launch-конфигурации между voice/semantic_map/mission
(комментарий `:26-43`).

### 8.3 Пространства имён

**НЕ НАЙДЕНО.** Grep `namespace=`/`PushRosNamespace` по всему репо
(кроме build/install/log) — 0 совпадений. Изоляция топиков делается через
`remappings=[...]` на отдельных `Node()`, напр.
`lidars.launch.py:573` (`("/scan", scan_topic)`),
`hardware.launch.py:206` (`("/diff_drive_controller/odom", "/odom")`).

> Обновлено под 2423411: перепроверено на HEAD — тот же grep даёт 0
> реальных совпадений (`PushRosNamespace` встречается только в
> `docs/launch_files_explained.md` как иллюстративный пример из общего
> ROS2-туториала, не в реальном launch-файле). Вывод не изменился.

### 8.4 Передача параметров

Оба варианта используются, иногда вместе в одном списке:
- Инлайн-dict: `hardware.launch.py:191-197`.
- YAML-путь + dict вместе: `hardware.launch.py:202-205`
  (`[{"robot_description": ...}, controllers_path]`).
- YAML-путь через `launch_arguments` во вложенный `IncludeLaunchDescription`:
  `desk.launch.py:34-38` → `high_level_stack.launch.py:393-397,419,437-438,456-457`.
- Кастомная Python-функция, генерирующая dict: `lidars.launch.py:28,626-644`
  (`merger_params(...)` из `dual_laser_merger_params.py`).
- Шаблонизированный конфиг `guide_robot_description`: генерируется
  `render_params.py` через `CMakeLists.txt:13-24` в
  `share/guide_robot_description/config/controllers.yaml`, читается
  `hardware.launch.py:138,203-204` (механизм и sibling-source-tree оговорка —
  `CLAUDE.md:61-66` этого репозитория).

### 8.5 Профили запуска

> Обновлено под 2423411: строки `DeclareLaunchArgument` в `hardware.launch.py`
> сдвинулись (см. полный рефреш в §8.1) — набор аргументов сам по себе не
> потерял ни одного значения, только добавил `launch_face`/`launch_llm`.
> **"kiosk"-режима как ROS launch-профиля по-прежнему НЕ НАЙДЕНО** (grep
> `-i "kiosk"` по launch-файлам `guide_robot_bringup` — 0 совпадений), но
> появился отдельный, не-ROS механизм kiosk-показа в `guide_robot_face` —
> описан ниже, полностью переписан этот подраздел.

Явные `DeclareLaunchArgument` под sim/real/mock (номера строк — см. §8.1
для `hardware.launch.py`): `use_sim_time` (форсируется `"true"` в
`simulation.launch.py:69,77,93,121,116`), `use_mock_hardware`, `real_lidars`
(`perception.launch.py:36-41`, не менялся), `slam`, `launch_high_level`,
`launch_face`/`launch_llm` (новые), `autostart`/`autostart_nav`/
`autostart_supervisor` (`high_level_stack.launch.py:74-79`,
`nav_stack.launch.py:68-79`, оба не менялись), `launch_foxglove`/
`launch_rviz`.

**Kiosk-показ лица — отдельный, не-ROS механизм в `guide_robot_face`**
(новые файлы `scripts/face_kiosk.sh`, `scripts/guide-robot-face.desktop`,
`scripts/guide-robot-noblank.desktop`):

- `face_kiosk.sh` (15 строк, полностью): ждёт, пока `http://127.0.0.1:8090`
  (или `$FACE_URL`, `face_kiosk.sh:9` — `URL="${FACE_URL:-http://127.0.0.1:8090}"`)
  ответит `200` (`curl -sf --max-time 2`, цикл `:12-14`, комментарий `:10-11`
  объясняет: `hardware.launch` стартует дольше минуты, иначе браузер
  навечно застревает на "не удаётся подключиться"), затем запускает
  `exec firefox --kiosk --new-instance "$URL"` (`:15`). Гашение
  скринсейвера — `xset s off`/`xset s noblank`/`xset -dpms` (`:6-8`).
- Дисплей **не выбирается явно нигде в этих трёх файлах** — ни
  `DISPLAY=`, ни `--display`, ни `:0` не встречаются (grep всего файла —
  0 совпадений); скрипт полагается на уже активную X-сессию, в которую
  его запускает XDG autostart.
- `guide-robot-face.desktop` (6 строк) — XDG/GNOME autostart-запись
  (`Type=Application`, `X-GNOME-Autostart-enabled=true` —
  `guide-robot-face.desktop:2,6`), `Exec=face_kiosk.sh` (`:5`, по имени,
  через `$PATH`). Чтобы подействовать, кладётся в
  `~/.config/autostart/guide-robot-face.desktop`, скрипт — в
  `~/.local/bin/face_kiosk.sh` (README-рецепт,
  `guide_robot_face/README.md:220-224`); установочный шаг `setup.py:19`
  (`(f"share/{PACKAGE_NAME}/scripts", glob("scripts/*"))`) кладёт файлы
  только в `share/guide_robot_face/scripts/` — копирование в
  `~/.config/autostart/`/`~/.local/bin/` **не автоматизировано**, это
  ручной шаг из README.
- `guide-robot-noblank.desktop` (6 строк) — отдельная autostart-запись,
  дублирующая те же `xset`-команды (`Exec=sh -c "xset s off; xset s
  noblank; xset -dpms"`, `:5`) независимо от `face_kiosk.sh` (на случай,
  если тот сам не успел/упал).
- Ни systemd user unit, ни `.service`-файла нигде нет — это чисто XDG
  autostart (`guide_robot_face/README.md:165-167` описывает механизм
  явно: логин `jetson` → autostart → `~/.local/bin/face_kiosk.sh` →
  Firefox kiosk на `:8090`, скрипт ждёт порт до 60с).
- **Однодисплейные допущения в текущем механизме** (факты, не
  рекомендации): единственный хардкод-URL на процесс (`face_kiosk.sh:9`);
  один `.desktop`-файл с фиксированным именем без параметризации под
  второй экземпляр (`guide-robot-face.desktop:1-6`); ни `DISPLAY`, ни
  `wmctrl`/`xdotool`/lock-файл/оконный заголовок нигде не
  используются (grep пакета на `wmctrl|xdotool|lockfile|flock|pidfile|
  window-position|xrandr` — 0 совпадений в коде, единственное упоминание
  `xrandr` — в README как заметка "когда воткнут второй HDMI —
  `xrandr` и `--window-position`, не этот пакет",
  `guide_robot_face/README.md:236-237`); README прямо говорит, что второй
  физический экран сейчас не подключён (`DFP-0: disconnected`) и его
  поддержка explicitly вне скоупа этого пакета. Значит: чтобы поднять
  второй kiosk-клиент на втором физическом экране, текущий механизм
  придётся трогать как минимум в части — выбора `DISPLAY`/`xrandr`-вывода
  (сейчас никак не выбирается), параметризации URL на инстанс (сейчас один
  `FACE_URL` на скрипт), и второй `.desktop`-записи с другим именем
  (сейчас имя `guide-robot-face.desktop` жёстко зашито в рецепте установки).
  Это факты о текущем механизме, не предложение архитектуры.

### 8.6 Конвенции нового пакета (на примере `guide_robot_voice`)

`setup.py` (структура, `guide_robot_voice/setup.py:9-37`):
```python
setup(
    name=PACKAGE_NAME, version="0.1.0", packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{PACKAGE_NAME}"]),
        (f"share/{PACKAGE_NAME}", ["package.xml"]),
        (f"share/{PACKAGE_NAME}/launch", glob("launch/*.launch.py")),
        (f"share/{PACKAGE_NAME}/config", glob("config/*.yaml")),
        (f"share/{PACKAGE_NAME}/models", glob("models/*.onnx") + ...),
    ],
    install_requires=["setuptools"], zip_safe=True,
    entry_points={"console_scripts": [f"tts_node = {PACKAGE_NAME}.tts_node:main", ...]},
)
```
`lib/`-модули без rclpy — `guide_robot_voice/guide_robot_voice/lib/*.py`
(`chunker.py`, `scheduler.py`, `sink.py`, `resampler.py`, `ring.py`,
`vad_hysteresis.py`, `dc_blocker.py`, `qos.py`-исключение) — правило
задокументировано `CLAUDE.md:108` этого репозитория. Node-файлы с rclpy —
на уровень выше.

Что удаляется из `ros2 pkg create`: `test_flake8.py`/`test_pep257.py`
отсутствуют в `guide_robot_voice/test/` (ruff их заменяет, `CLAUDE.md:40`),
но **непоследовательно** — `guide_robot_supervisor/test/test_flake8.py` и
`test_pep257.py` всё ещё существуют. `resource/<package_name>` — пустой
0-байтовый ament-маркер, паттерн одинаков во всех пакетах.

### 8.7 `--symlink-install` — статус

Корневые инструкции используют флаг без оговорок (`CLAUDE.md:18`,
`README.md:51`). Единственный **документированный** гоча —
для C++/pybind11-пакета `guide_robot_sonar`, не для plain-setuptools:
- `guide_robot_sonar/CMakeLists.txt:19-31` — комментарий про `sys.path[0]`
  = realpath скрипта при `--symlink-install`, из-за чего `.so`-модуль рядом с
  symlink'ом не подхватывается автоматически.
- `guide_robot_sonar/scripts/sonar_node_mult.py:53-54` — тот же комментарий
  в коде.
- `guide_robot_sonar/README.md:11-19` — то же самое по-русски, с явным
  "проверено на железе".

**Для чистых `ament_python`/setuptools-пакетов (как предполагаемый
`guide_robot_operator_ui`) документированной поломки `--symlink-install`
НЕ НАЙДЕНО** — grep `symlink` по `.md/.py/.cfg/.txt` всего репо не даёт
других совпадений, кроме случая `guide_robot_sonar` и двух нейтральных
упоминаний команды сборки.

> Обновлено под 2423411: `guide_robot_mission_control/pyproject.toml` и
> `guide_robot_semantic_map/pyproject.toml` действительно правились
> (по 1 строке в `[tool.ruff.lint].ignore` каждый — добавлены `ANN101`,
> `ANN102`, правила "нет аннотации типа для `self`/`cls`"). Оба файла
> целиком — только `[tool.ruff...]`-таблицы, **нет ни `[build-system]`,
> ни setuptools-секции ни в одном из них**. Это чисто линт-конфигурация
> ruff, **никак не связанная** с `--symlink-install`/setuptools-упаковкой
> — вывод §8.7 не меняется, поломки для `ament_python`-пакетов по-прежнему
> НЕ НАЙДЕНО.

---

## 9. Расхождения и открытые вопросы

Ничего из перечисленного не решается этим документом — все пункты требуют
решения человеком, не автора отчёта.

1. **"Стоп тура" не имеет отдельного ROS-интерфейса** — это действие
   `cancel_goal_async()` на action `run_tour` (§1.4, §2.2). Если оператору
   нужна кнопка "Стоп", UI должен либо хранить последний `goal_handle` (что
   предполагает, что UI сам стартовал тур или как-то получил handle того же
   голa от другого клиента — а `ActionClient` в ROS2 не даёт третьей стороне
   взять чужой активный `goal_handle` без знания его `goal_id`), либо
   решение нужно, как получать/шарить `goal_id` активного тура между
   клиентами (`guide_robot_llm`/CLI/UI). **Требует решения.**
2. **Немедленный стоп не проверяет владельца/источник.** Любой клиент,
   знающий `goal_id`, может отменить тур в любой момент, включая момент
   ожидания подтверждения оператором (§2.2). Нужно ли это разрешать без
   аутентификации с операторского UI — вопрос политики, не кода.
3. **Гранулярность прогресса тура — только уровень экспоната/остановки**,
   не чанка/фразы (§3.1, §3.4). Поля `chunk_index`/`chunk_total` в
   `MissionState.msg` **объявлены, но не заполняются** нигде в коде — это
   явное расхождение между схемой сообщения и её реализацией. Если
   пословная/почанковая синхронизация слайдов нужна, придётся либо (а)
   добавить publish `Narrate.Feedback` в `mission_fsm`/`narration_server`
   и прокинуть в `MissionState`, либо (б) смириться с гранулярностью
   "меняем слайд по смене `exhibit_id`".
4. **Нет команды "ехать на базу" как самостоятельного действия** — только
   `return_home` внутри `RunTour.Goal` (§5.1). Кнопка "На базу" в UI не
   может быть реализована как независимый вызов — либо это должен быть
   отдельный `RunTour` с пустым списком остановок и `return_home=true`
   (нужно проверить, обработает ли `_resolve_tour` пустой `location_ids` +
   пустой/несуществующий `tour_id` как ошибку — судя по коду §2.1,
   `_resolve_tour` вернёт `None` и тур будет `ABORTED` при пустом
   `location_ids` и нерезолвящемся `tour_id` — то есть **текущий код не
   поддерживает "просто поехать домой" без тура**), либо нужен новый
   отдельный интерфейс. **Требует решения архитектора/пользователя, не
   покрывается разведкой.**
5. **Нет runtime-механизма сброса локализации** (`/initialpose` никем не
   публикуется/не обрабатывается, §6.2). Кнопка "Сброс локализации" в UI
   потребует либо (а) полагаться на голый nav2_amcl `/initialpose`
   (стандартный ROS-топик, `PoseWithCovarianceStamped`) без какой-либо
   доп. логики консистентности с EKF/одометрией (которых, к слову, тут и
   нет — §6.1), либо (б) писать новый механизм с нуля. Ни один из вариантов
   не описан существующим кодом — решение нужно принимать сейчас, а не
   опираться на "уже что-то есть".
6. **AMCL — единственный локализационный бэкенд по умолчанию**, EKF
   отсутствует полностью (§6.1). Планирование сброса локализации не должно
   исходить из предположения о существовании EKF-состояния для сброса.
7. **Медиа-полей для слайдов/видео в semantic_map нет вообще** (§4.2) —
   вся инфраструктура (схема YAML, `GetExhibitContent`/`ExhibitChunk`) не
   предполагает медиа. Расширение схемы контента, добавление поля вроде
   `slide_url`/`media` в `ExhibitChunk.msg` и/или в `content/*.yaml` —
   это решение по дизайну сообщений, которое требует **пересборки**
   `guide_robot_msgs` (per `CLAUDE.md:8` этого репозитория — "build
   interfaces first when adding new message types" в другом проекте,
   но тот же принцип верен и здесь: `guide_robot_msgs` перед
   зависимыми пакетами).
8. **`face_server.py` уже написан как переиспользуемый aiohttp-класс без
   ROS**, но занимает порт с непроверенным на Jetson конфликтом (§7.1,
   §7.7). Общий процесс с operator_ui или раздельный — открытый вопрос,
   факты за/против собраны в §7.7 без рекомендации, как и требовалось.
9. **README `guide_robot_bringup` устарел** — говорит про "семь
   launch-файлов" (`README.md:17`), на диске теперь **девять** (§8.1). Не
   относится напрямую к operator_ui, но по конвенции репозитория
   (`CLAUDE.md`: "Trust the code over the README, and fix the README when
   you notice drift") это стоит поправить отдельным PR, не в рамках данной
   разведки. **Обновлено под 2423411: расхождение усугубилось** — README
   не только не поправлен, но и вообще не упоминает новый
   `teleop.launch.py` (grep README на "teleop" — 0 совпадений), и не
   упоминает новую зависимость `guide_robot_llm` в разделе
   "Зависимости" (`README.md:206-213`), хотя `package.xml` её уже содержит.
10. **`AskUser.action`, `NarrationControl.srv` (production), `SetFaceState.srv`
    (клиент), `Doa.msg`, `SonarRanges.msg`, `SystemEvent.msg` (подписчик)**
    — все перечислены как объявленные, но не имеющие полной пары
    publisher/subscriber или client/server реализации (§1.2-1.3); ни один
    файл, где это проверялось, не попал в диапазон `4f9b00a..2423411`,
    так что для этих шести пунктов вывод **не изменился, все ещё открыты**.
    Если operator_ui планирует полагаться на любой из них — нужно уточнить,
    действительно ли они будут реализованы к моменту интеграции.
    **`DialogPhase.msg` — закрыт, см. "Снято дельтой" ниже.**
11. **"Kiosk"-режима launch не существует** (§8.5) — если operator_ui
    подразумевает отдельный launch-профиль (полноэкранный браузер на
    планшете и т.п.), такой профиль придётся создавать с нуля, это не
    "доделать существующее". **Обновлено под 2423411: частично снято, см.
    "Снято дельтой" ниже** — как ROS launch-arg kiosk-режима по-прежнему
    нет, но появился рабочий не-ROS механизм (X11 autostart +
    `face_kiosk.sh`) в `guide_robot_face`, который можно взять за образец
    (не переиспользовать напрямую — см. однодисплейные допущения §8.5).

### Снято дельтой (`4f9b00a..2423411`)

**Закрыто / получило ответ:**

- **Пункт 11 (kiosk не существует) — частично снят.** Рабочий kiosk-показ
  теперь есть: `guide_robot_face/scripts/face_kiosk.sh` +
  `guide-robot-face.desktop` + `guide-robot-noblank.desktop` (§8.5). Не
  ROS-профиль, а X11 XDG-autostart — но это конкретный, документированный,
  установленный на железе механизм (README перечисляет реальный путь на
  Jetson: `~/.local/bin/face_kiosk.sh`, `~/.config/autostart/guide-robot-face.desktop`).
- **§7.3 (resolver/агрегатор "НЕ НАЙДЕНО") — полностью снят.**
  `SetFaceState.srv`'s старый комментарий про "агрегатор на stage 2" —
  реализован: `aggregator_node.py` + `lib/aggregator.py` (§7.3).
- **Пункт 10, `DialogPhase.msg` — закрыт.** Раньше был только тестовый
  подписчик (`test_dialog_phase_publisher.py`), теперь есть настоящий
  production-потребитель: `aggregator_node.py:63` подписан на
  `/dialog/phase` и использует `dialog_phase` как один из входов
  приоритетной схемы (§7.3).
- **Пункт 8 (порт `guide_robot_face` — риск конфликта на Jetson не
  подтверждён) — снят на уровне утверждения, не гарантии.** Комментарий
  `config/face_node.yaml:1` теперь заявляет порт 8090 свободным на
  конкретном IP (`10.100.20.169`); это по-прежнему не код-гарантия
  (никакой проверки в репозитории нет), но исходный открытый риск был
  сформулирован как "не подтверждено", а теперь он явно "заявлено
  подтверждённым" — статус изменился, стоит различать в дальнейшем
  планировании.

**Появилось новое (не было в отчёте на `4f9b00a`):**

- **Второй экран для kiosk — новый открытый вопрос.** Текущий
  kiosk-механизм жёстко однодисплейный (единственный `FACE_URL`,
  единственная `.desktop`-запись без параметризации, отсутствие
  `DISPLAY`/`xrandr`-выбора, README прямо говорит "второй экран — не
  этот пакет"). Если operator_ui должен показываться на отдельном,
  втором физическом экране — это прямо не поддерживается ничем
  существующим и не было видно на `4f9b00a` (тогда механизма не было
  вообще). См. §8.5.
- **Третий (и по факту минимум четвёртый) клиент `Say.action` — новый
  открытый вопрос про приоритеты озвучки.** `guide_robot_bt_nodes`'s
  `SayAction` шлёт голоса с `priority=SAFETY=200` изнутри `bt_navigator`
  (§1.1, §5.6) — численно выше `PRIORITY_NARRATION=50`, которым пользуется
  весь тур-нарратив. Не определено статическим анализом, действительно ли
  и как это может прервать активный рассказ во время тура — открытый
  вопрос, требующий рантайм-проверки, не входит в компетенцию recon.
- **`/supervisor/estop` теперь реально гейтит cmd_vel через `mux_final`'s
  lock (priority 255)** — это меняет модель "что произойдёт при
  экстренной остановке от operator_ui, если такая кнопка появится":
  раньше estop влиял только на `mission_fsm_node.py`'s
  `safety_hold_event`; теперь есть независимый аппаратный путь через
  twist_mux (§5.5). Открытый вопрос: если operator_ui получит кнопку
  "экстренный стоп", должна ли она публиковать `/supervisor/estop`
  напрямую (используя новый lock) или идти через `mission_fsm`.
- **`/admin_cmd_vel` — новый привилегированный путь, обходящий e_stop и
  collision_monitor** (`teleop.launch.py:10-13`, "admin bypass"). Если
  operator_ui когда-либо получит функцию ручного управления движением —
  это прямая, явно описанная в коде точка, которая **отключает штатную
  защиту**; на `4f9b00a` этого пути не существовало. Существенный
  safety-момент для дизайна, а не просто техническая деталь.
- **Новая группа `llm` и переупорядочивание supervisor'а** (`voice`
  теперь стартует до `navigation`, потому что `bt_navigator`'s
  `SayAction`-нода требует живой `"say"`-action-сервер уже на старте —
  `guide_robot_supervisor/config/supervisor.yaml:18-31`, комментарий
  прямо это объясняет). Меняет граф готовности сервисов при бут-апе —
  релевантно для того, когда именно operator_ui может считать бэкенд
  "готовым".
- **AMCL `initial_pose.yaw` изменился с `0.0` на `π`, с явным
  комментарием про доковую позу** (§6.2) — не меняет вывод "нет
  runtime-сброса позы" (пункт 5), но впервые явно фиксирует в коде, что
  `(0,0,π)` — это конкретно поза зарядного дока, а не произвольный
  ноль. Стоит иметь в виду при проектировании кнопки "Сброс локализации".
