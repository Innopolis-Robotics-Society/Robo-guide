# guide_robot_mission_control

Владелец состояния тура. Три `rclpy.lifecycle.LifecycleNode`
(`mission_fsm`, `narration_server`, `presence_monitor`) + CLI + опциональный
контейнер, объединяющий первые два в один процесс. Пакет водит
`guide_robot_voice` (`Say`) и `guide_robot_semantic_map` (контент, локации,
туры, маршруты) через их action/srv-интерфейсы; сам речь не синтезирует,
текст не пишет, картой не владеет. Работает без ЛЛМ: полный тур с
прерываниями гоняется из CLI и из тестов на моках.

`ament_python`, ROS 2 Humble. Практический справочник по факту реализации —
см. также `guide_robot_mission_design.md` (черновик v1) и раздел
«Отличия» ниже про то, где реализация от него разошлась.

## Топология

```
                        /supervisor/estop (Bool), /supervisor/state (String)
                                          │
RunTour (action) ──►┌─────────────────┐◄─┘
                     │   mission_fsm   │──► NavigateToPose (action, Nav2)
~/request_pause      │                 │──► ListTours, ListLocations
~/request_resume     └────────┬────────┘     (guide_robot_semantic_map)
~/submit_confirm              │
   (сервисы)          narrate (action)
                               │
                     ┌─────────▼───────┐
                     │ narration_server │──► say (action, tts_node)
                     │                 │──► /speech/cancel_all (pub+sub)
                     └────────┬────────┘──► ~/get_exhibit_content
                               │              (guide_robot_semantic_map)
                        ~/control (NarrationControl -- есть, но
                        mission_fsm его не вызывает, см. «Грабли»)

/speech/wakeword ──►┌──────────────────┐
/asr/transcript ──► │ presence_monitor │──► /mission/presence
/vad, /voice/speaking└──────────────────┘    (публикуется, mission_fsm
/mission/state (опц.)                         НЕ подписан, см. «Грабли»)
```

`mission_fsm` и `narration_server` вместе образуют «горячий путь»
barge-in → пауза нарратива — отсюда `mission_container` (см. ниже).
`presence_monitor` в этот путь не входит и всегда отдельный процесс.

## Ноды

### `mission_fsm`

Владелец `/mission/state` и стека прерываний. Один активный `RunTour`-goal
исполняется целиком внутри `execute_callback` (не в персистентном фоновом
потоке — второй одновременный `RunTour` получает `REJECT` на уровне
`goal_callback`, не preempt: тур — это владение роботом целиком).
`FsmContext` (`fsm/context.py`) создаётся заново на каждый `RunTour`-goal и
живёт ровно его исполнение; `safety_hold_event`/`deactivating_event` —
общие для узла и переживают несколько последовательных туров за одну
активацию lifecycle.

**Action-сервер**: `run_tour` (`RunTour`) — `tour_id` разворачивается в
остановки через `ListTours(tour_id).stops`, если `location_ids` пуст;
`location_ids` — прямой override без похода в semantic_map.

**Сервисы** (тонкие обёртки над теми же хуками, что использует test/ —
design §5.4 п.6, нет реального ASR/LLM): `~/request_pause`,
`~/request_resume` (`std_srvs/Trigger`), `~/submit_confirm`
(`std_srvs/SetBool`). Добавлены на шаге 8, чтобы `mission_cli` мог
достучаться до них из отдельного процесса — голые методы узла
(`submit_answer`/`submit_confirm`/`request_pause`/`request_resume`) видны
только тестам, которые держат ссылку на сам объект ноды.

**Клиенты**: `narrate` (`Narrate`, к `narration_server`), `say` (`Say`,
напрямую — приветствие `GREETING` и вопрос «Идём дальше?»
`AWAITING_CONFIRM`, оба мимо `narration_server`), `navigate_to_pose`
(`NavigateToPose`, Nav2), `/location_server/list_tours`,
`/location_server/list_locations`.

**Подписки**: `/speech/cancel_all` (barge-in по `reason==REASON_BARGE_IN`),
`/supervisor/estop`, `/supervisor/state` (safety hold, см. ниже).

**Параметры**: `answer_max_s`(45), `confirm_timeout_s`(20),
`confirm_repeat_max`(1), `nav_stop_timeout_s`(180), `pause_timeout_s`(120),
`held_max_s`(300), `poll_period_s`(0.02), `hard_stop_result_timeout_s`(1.0),
`heartbeat_s`(1.0), `service_call_timeout_s`(2.0), `language`("ru"),
`greeting_text`, `confirm_question_text`, `home_frame`("map"),
`home_pose`([x,y,yaw]).

### `narration_server`

Один `Narrate`-goal = один `Say`-goal на один готовый элемент
`GetExhibitContent.chunks` — сам текст не режет (design §0.5, §4.1:
чанкование отменено, `chunks[]` уже написаны и провалидированы ревьюером
в `guide_robot_semantic_map`). Учёт произнесённого — `chunk_plan.py`
(`ChunkPlan`), решение «откуда продолжать» — `resume.py`
(`resolve_resume`/`apply_resume_policy`, три политики:
`repeat_chunk`/`continue_next`/`overlap_1`).

**Action-сервер**: `narrate` (`Narrate`) — `exhibit_id` обязателен
(ключ для `GetExhibitContent` и валидации resume), `text` — опциональный
прямой override (пропускает поход в semantic_map, используется
`mission_cli say`).

**Сервис**: `~/control` (`NarrationControl`) — `MODE_SOFT` (доиграть текущий
чанк + уже отправленный lookahead, дальше не слать) / `MODE_HARD`
(`CancelAll(scope=narration)` + отмена активных `Say`). Реализован
полностью, но **`mission_fsm` его не вызывает** — см. «Известные грабли».

**Публикует/слушает**: `/speech/cancel_all` (оба направления — сам шлёт
на `MODE_HARD`/barge-in-эхо, и реагирует на чужой с `reason==REASON_BARGE_IN`).

**Параметры**: `lookahead`(1), `resume_policy`("repeat_chunk"),
`resume_bridge_enabled`(true), `resume_bridge_text`("Продолжаю."),
`soft_pause_max_s`(8.0), `hard_stop_result_timeout_s`(0.3),
`say_priority`/`say_scope` (= `Say.Goal.PRIORITY_NARRATION`/`SCOPE_NARRATION`),
`content_language`("ru"), `content_mode`("full"), `service_call_timeout_s`(2.0).

### `presence_monitor`

Агрегирует разрозненные свидетельства присутствия в один `/mission/presence`
(`Presence`, `PresenceTracker` в `presence.py` — чистая логика, `present`
взводится немедленно любым свидетельством и снимается только после
`disengage_timeout_s` без новых). Источники — реальные топики
`guide_robot_voice`: `/speech/wakeword`, `/asr/transcript` (только
`is_final`), `/vad` + `/voice/speaking` (для гейта «не путать свою же
речь с VAD без AEC», `vad_evidence_allowed()`). `/perception/people` из
design §6 не подключается вовсе — под него нет типа сообщения в
`guide_robot_msgs`, «не ломаться при отсутствии» реализовано как
«не создавать подписку», а не рантайм-защита.

**Публикует**: `/mission/presence` (`Presence`, heartbeat `publish_rate_hz`).

**Параметры**: `disengage_timeout_s`(120), `wakeword_min_confidence`(0.6),
`ignore_vad_while_speaking`(true), `tts_tail_ms`(300), `sources`
(`["wakeword","asr_final","vad"]`), `publish_rate_hz`(1.0),
`mission_state_weak_evidence`(false, наше расширение — design §6 упоминает
`/mission/state` как опциональное слабое свидетельство, но не
специфицирует параметр в §8).

## FSM тура (`mission_fsm/fsm/`)

`RootStateMachine.run_tour()` (`fsm/root_sm.py`) — таблица переходов
`_TRANSITIONS`, стартует в `GREETING` (или сразу `NAVIGATING`, если
`tour.greet=False`), завершается на исходе, не ведущем никуда.
`InterruptibleState` (`fsm/base.py`) — базовый класс: 20-мс поллинг
(`poll_period_s`), `CANCELED`/`HELD` детектятся ЕДИНООБРАЗНО для ЛЮБОГО
состояния самой базой (design §5.4 правило 5 — HELD вытесняет всё), не
каждым `poll()` по отдельности. Неизвестный исход состояния — `RuntimeError`
из `run_tour()`, не тихий выход (обнажило реальный баг с `Narrate`
REJECTED — см. «Известные грабли»).

**Состояния** (`fsm/states/`): `greeting`, `navigating`, `narrating`,
`answering`, `awaiting_confirm`, `paused`, `held`, `returning`.
`navigating`/`narrating` не абортят тур на сбое — пропускают остановку
(`stops_skipped++`, `NAV_FAILED`/`NARRATE_FAILED`) и едут к следующей.

**Стек прерываний глубины 1** (`interrupt_stack.py`, design §5.4) — не
структура «стек» в общем смысле, ровно один слот; второй одновременный
запрос на прерывание — явный `StackBusyError`, не очередь. `answer`-фрейм
(barge-in во время `NARRATING`/`GREETING`/`NAVIGATING`) и `confirm`-фрейм
(`AWAITING_CONFIRM`) хранят `base_state`+`resume_token`, чтобы `resume_base`
(псевдо-переход в `root_sm`) знал, куда вернуться. `HELD` фрейм НЕ трогает
(design правило 5) — safety-стоп посреди `AWAITING_CONFIRM` восстанавливает
именно confirm-фрейм после `SAFETY_CLEAR`, не роняет его.

**`resume_token`** (`resume.py`) — грамматика
`v1|<exhibit_id>|<version>|<chunk_idx>|<char_off>`, чистые функции без
ROS: и `narration_server`, и `mission_fsm`, и тесты обязаны получать один
и тот же ответ на «откуда продолжать».

## Интерфейсы (сводно)

| Интерфейс | Тип | Нода |
|---|---|---|
| `run_tour` | `RunTour` (action) | mission_fsm |
| `~/request_pause`, `~/request_resume` | `std_srvs/Trigger` | mission_fsm |
| `~/submit_confirm` | `std_srvs/SetBool` | mission_fsm |
| `/mission/state` | `MissionState` (pub, TRANSIENT_LOCAL depth 1) | mission_fsm |
| `narrate` | `Narrate` (action) | narration_server |
| `~/control` | `NarrationControl` | narration_server (не вызывается mission_fsm) |
| `/speech/cancel_all` | `CancelAll` (pub+sub) | mission_fsm, narration_server |
| `/mission/presence` | `Presence` (pub, TRANSIENT_LOCAL depth 1) | presence_monitor |
| `/supervisor/estop`, `/supervisor/state` | `Bool`/`String` (sub) | mission_fsm |

QoS-профили — `lib/qos.py`, единственный модуль пакета, которому разрешено
импортировать `rclpy` из «чистых» модулей верхнего уровня.

## Чистая логика без ROS

Тестируется без поднятого rclpy, отдельно от узлов (design: узел
только раскладывает ROS-msg по полям чистых функций).

| Модуль | Что делает |
|---|---|
| `resume.py` | Грамматика `resume_token`, `resolve_resume`/`apply_resume_policy` |
| `chunk_plan.py` | Учёт произнесённого по чанкам одного `Narrate` (`ChunkPlan`) |
| `interrupt_stack.py` | Стек прерываний глубины 1 (`InterruptStack`) |
| `presence.py` | `PresenceTracker`, гейт VAD-во-время-своей-речи |

## CLI (`mission_cli`)

```bash
ros2 run guide_robot_mission_control mission_cli tour --tour lab_demo [--no-confirm] [--locations id1,id2]
ros2 run guide_robot_mission_control mission_cli status [--once]
ros2 run guide_robot_mission_control mission_cli pause [--hard]   # --hard = синтетический /supervisor/estop
ros2 run guide_robot_mission_control mission_cli resume
ros2 run guide_robot_mission_control mission_cli confirm yes|no
ros2 run guide_robot_mission_control mission_cli say "текст"       # прямой Narrate, без тура
ros2 run guide_robot_mission_control mission_cli barge [--scope narration|dialog|all|safety]
```

`ask` из design §11 (отправка `AskUser`-goal со свободным вопросом и
`option_phrases`) сознательно не реализован — `AskUser`-сервер в
`mission_fsm` не существует (отложено вместе с ASR/LLM-матчингом,
подтверждение в v1 идёт только через `confirm`). `barge`/`pause --hard` —
синтетические сигналы для ручной отладки путей, у которых пока нет
реального источника (VAD, presence).

## Запуск

```bash
# mission_fsm + narration_server одним процессом (design §1, дефолт),
# presence_monitor отдельно; ноды unconfigured -- подъём вручную или
# через supervisor
ros2 launch guide_robot_mission_control mission.launch.py

# автоподъём в порядке presence_monitor -> narration_server -> mission_fsm
ros2 launch guide_robot_mission_control mission.launch.py autostart:=true

# три отдельных процесса вместо mission_container -- удобнее для отладки
ros2 launch guide_robot_mission_control mission.launch.py use_container:=false
```

Ручной подъём (`autostart:=false`):

```bash
ros2 lifecycle set /presence_monitor configure && ros2 lifecycle set /presence_monitor activate
ros2 lifecycle set /narration_server configure && ros2 lifecycle set /narration_server activate
ros2 lifecycle set /mission_fsm configure && ros2 lifecycle set /mission_fsm activate
```

Через супервизор (`guide_robot_supervisor`, группа `mission`, `requires: [navigation]`):

```bash
# nav_stack.launch.py уже поднимает mission.launch.py (autostart:=false)
# и supervisor.launch.py вместе с safety/localization/navigation
ros2 launch guide_robot_bringup nav_stack.launch.py
ros2 service call /supervisor/bringup std_srvs/srv/Trigger   # если autostart_supervisor:=false
```

## Известные грабли

- **`launch_ros.actions.Node(name=...)` на многоузловом контейнере ломает
  имена внутренних узлов.** `name="mission_container"` на `Node`-экшне
  `mission_container` заставлял launch_ros добавить `--ros-args -r
  __node:=mission_container` — глобальный remap, применяющийся сразу к
  ОБОИМ узлам процесса (`MissionFsmNode`/`NarrationServerNode` хардкодят
  своё имя в `super().__init__()`, но `__node`-remap переопределяет любое
  переданное имя на уровне rcl). Итог: оба узла регистрировались в графе
  как `/mission_container`, `lifecycle_manager_mission` вечно ждал
  `narration_server/get_state`, которого не существовало (воспроизведено
  вживую). Исправлено — `name=` на этом `Node`-экшне убран.
- **`PAUSED` обязан сам останавливать активный `Narrate`.** `HELD`/
  `CANCELED` детектятся базой (`fsm/base.py`) и сама база вызывает
  `cancel_active_work()`; `PAUSED` производится `NarratingState.poll()`
  напрямую и раньше НЕ звала `cancel_active_work()` — брошенный
  `Narrate`-goal держал `narration_server` «занятым», и после `resume`
  новый `Narrate` получал `OUTCOME_REJECTED("busy")`, что через
  `NARRATE_FAILED` тихо пропускало остановку и заканчивало тур раньше
  времени (воспроизведено вживую: `pause` → `resume` → тур молча уехал
  домой). Исправлено — `poll()` теперь сам зовёт `cancel_active_work()`
  перед возвратом `PAUSED`.
- **`_TRANSITIONS["narrating"]` не знал про `ABORTED`.** `Narrate`
  может вернуть `OUTCOME_REJECTED`/`OUTCOME_ABORTED` (контент не найден —
  реальный случай, `content_server` не выдумывает текст для остановок без
  фикстуры), а `root_sm` ронял необработанный `RuntimeError` вместо того,
  чтобы пропустить остановку. Исправлено — зеркалит `NAV_FAILED`.
- **`presence_monitor` публикует, `mission_fsm` не подписан.** `/mission/
  presence` существует и агрегирует реальные свидетельства, но
  `PausedState`/`request_pause`/`request_resume` в mission_fsm — только
  тестовые хуки (design §5.4 п.6, нет ASR/LLM). Живая интеграция
  presence → `PAUSED` — открытый TODO, не реализована.
- **`NarrationControl.srv` (`MODE_SOFT`/`MODE_HARD`) существует, но
  `mission_fsm` его не вызывает.** Текущий `PAUSED`/`HELD`/`CANCELED`
  жёстко отменяет активный `Narrate` (`cancel_goal_async`, MODE_HARD-
  эквивалент) даже там, где `MODE_SOFT` (доиграть чанк) был бы мягче для
  посетителя. Осознанное временное решение — см. докстринг
  `cancel_active_work()` в `fsm/states/narrating.py`.
- **`ROS_DOMAIN_ID` общий — изолированные тесты не защищены от живого
  стека.** `test/mocks/harness.py` поднимает каждый тест на отдельном
  `rclpy.Context()`, но это НЕ отдельный DDS-домен — топики/action/сервисы
  с теми же именами (`run_tour`, `narrate`, `navigate_to_pose`) от реально
  запущенного `mission.launch.py` в том же контейнере видны тестовому
  процессу и наоборот («There may be more than one action server...»,
  воспроизведено вживую). При параллельной ручной отладке и прогоне
  тестов — `ROS_DOMAIN_ID=<другое число>` для одного из них.
- **`config/mission.yaml` — плоские параметры, не вложенные группы
  design §8.** Черновик описывал `tour:`/`timeouts:`/`interrupts:` как
  YAML-секции; реализация объявляет всё плоско (`declare_parameter` на
  верхнем уровне каждого узла) — `config/mission.yaml` следует коду.
- **`phrases_ru.yaml` не существует.** Design §1/§8 предполагал отдельный
  файл системных фраз; в реализации `greeting_text`/`confirm_question_text`
  (mission_fsm) и `resume_bridge_text` (narration_server) — обычные
  ROS-параметры на своих узлах, без отдельного загрузчика.
- **Зарегистрирован в `guide_robot_supervisor` — единственный из
  сервисного слоя.** В отличие от `guide_robot_voice`/`guide_robot_llm`/
  `guide_robot_semantic_map` (сознательно вне `supervisor.yaml`), для
  `mission_control` design §10 реализован буквально: группа `mission`
  (`requires: [navigation]`, `optional: true`) добавлена и в
  `supervisor.yaml`, и в `supervisor_slam.yaml`. `lifecycle_manager_mission`
  в `mission.launch.py` теперь запускается БЕЗУСЛОВНО (раньше — только под
  `autostart:=true`) с `autostart`, проброшенным как launch-arg (default
  `false`) — тот же паттерн, что и `lifecycle_manager_safety` в
  `guide_robot_navigation/launch/common.launch.py`: сервис `~/manage_nodes`
  должен существовать независимо от того, кто инициирует `STARTUP`.
  `guide_robot_bringup/launch/nav_stack.launch.py` подключает
  `mission.launch.py` (`launch_mission:=true` по умолчанию, `autostart:=false`
  всегда — bring-up только через супервизор). `optional: true` — отказ этой
  группы не должен переводить весь supervisor в `FAULT` и блокировать уже
  поднятый safety/localization/navigation.
- **Полный прогон тестов подряд несколько раз — редкие segfault/таймауты
  discovery.** Не баг этого пакета: пред-существующая чувствительность
  тестовой инфраструктуры (`test/mocks/harness.py`, общий для
  `narration_server`/`presence_monitor` с шага 3) к нагрузке при частых
  повторных запусках `pytest` подряд в одном контейнере. Одиночные
  прогоны и прогон конкретного файла стабильно зелёные.

## Тесты

```bash
cd guide_robot_mission_control
python3 -m pytest test/ -q
ruff check .
```

718 тестов (717 проходят + 1 skip), без ROS-железа и без Gazebo — только
rclpy + моки (`test/mocks/`: `mock_nav_server.py`, `mock_say_server.py`,
`mock_semantic_map.py`, `sim_clock.py` — публикатор `/clock`, чтобы
120-секундные таймауты присутствия проверялись за миллисекунды).
`test_narration_resume.py` и `test_interrupt_stack.py` — блокирующие для
мержа (design §9.3). `test/mission_fsm_test_helpers.py` — общая сборка
реальных `mission_fsm`+`narration_server` поверх моков, единственный
шаренный тестовый хелпер такого рода в пакете (осознанное исключение из
конвенции «без общих хелперов между тестовыми файлами»).

| Файл | Что проверяет |
|---|---|
| `test_resume_token.py` | Грамматика `resume_token`, политики резюме |
| `test_chunk_plan.py` | Инвариант полноты `ChunkPlan`, property-based |
| `test_narration_resume.py` | Прерывание на каждом чанке, все `resume_policy`, lookahead 0/1 |
| `test_interrupt_stack.py` | Стек прерываний + FSM-уровень barge-in/answer |
| `test_tour_flow.py` | Полный тур, рестарт с индекса, skip при отказе nav/narrate, отмена, pause/resume |
| `test_safety_hold.py` | `SAFETY_HOLD` в `NARRATING`, `held_max_s`, фрейм переживает `HELD` |
| `test_presence.py` | Взведение по источникам, снятие через `disengage_timeout_s`, VAD-гейт |
| `test_mocks_smoke.py` | Моки сами по себе (epoch-fencing, cancel-семантика) |

## Отличия от `guide_robot_mission_design.md`

Design §0.5 сам документирует реконсиляцию с фактическим состоянием
репозитория на момент начала реализации (Say.action без
изменений, narration_server не чанкует, `content_id`→`exhibit_id`,
транзитный контент не существует, `/system/events`→`/supervisor/estop`+
`/supervisor/state`, `/speech/barge_in`→`reason==REASON_BARGE_IN` на
`/speech/cancel_all`, CancelAll без изменений) — не повторяется здесь.
Накопилось при самой реализации:

1. **`IDLE` не отдельный узел FSM.** `RunTour.execute_callback` заводит
   `RootStateMachine.run_tour()` напрямую с `GREETING`/`NAVIGATING`, без
   персистентного фонового цикла в `IDLE` — тот же паттерн, что у
   `narration_server._execute_narrate`.
2. **Транзитный нарратив (design §5.6) не реализован вовсе.** `NAVIGATING`
   только ведёт `NavigateToPose`, без параллельного `DROPPABLE`-`Narrate`
   на ходу — по явному запросу при планировании шага 7.
3. **`AskUser`-сервер не реализован.** Подтверждение (`AWAITING_CONFIRM`)
   и ответ (`ANSWERING`) в v1 идут через тестовые хуки
   (`FsmContext.submit_confirm()`/`submit_answer()`), а не через реальный
   ASR/LLM-матчинг `option_phrases` — design §5.4 п.6 оговаривает это
   явно как временное решение.
4. **`config/mission.yaml` плоский, `phrases_ru.yaml` не существует** —
   см. «Известные грабли».
5. **`NARRATE_FAILED` — исход, которого нет в design §5.2.** Добавлен,
   чтобы `Narrate` REJECTED/ABORTED (контент не найден и т.п.) пропускал
   остановку симметрично `NAV_FAILED`, а не ронял тур — см. «Известные
   грабли».
6. **`mission_cli confirm`** — подкоманда сверх списка design §11,
   появилась вместе с `~/submit_confirm`-сервисом (нужна была ручная
   проверка `AWAITING_CONFIRM` без ASR); `ask` из того же списка, наоборот,
   не реализован — см. выше.
7. **Регистрация в `guide_robot_supervisor` (группа `mission`) реализована
   буквально по design §10** — в отличие от `voice`/`llm`/`semantic_map`,
   которые остаются вне `supervisor.yaml` по прецеденту; см. «Известные
   грабли».
