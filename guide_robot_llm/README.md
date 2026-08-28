# guide_robot_llm

ЛЛМ-агент робота-экскурсовода. Три `rclpy.lifecycle.LifecycleNode`
(`tool_broker`, `dialog_agent`, `interaction_log`), каждый — отдельный
процесс. Пакет ведёт диалог и дёргает `guide_robot_mission_control`
(`RunTour`, `~/request_pause`/`~/request_resume`/`~/submit_confirm`/
`~/submit_answer`) и `guide_robot_semantic_map` (`ListLocations`/
`ListTours`/`EstimateRoute`) через их action/srv-интерфейсы; сам речь не
синтезирует, картой не владеет, состояние тура не хранит. Инференс —
внешний HTTP-сервер (`../llm_server/`, OpenAI-совместимый `/v1/chat/
completions`), не ROS-нода и не зависимость этого пакета.

`ament_python`, ROS 2 Humble. Практический справочник по факту
реализации — см. также `DIALOG_REWORK_PLAN.md` (план переработки
диалогового слоя, по которому построена текущая реализация).

## Ход диалога: действие → исполнение → реплика (два вызова ЛЛМ, не ReAct-цикл)

Один финальный транскрипт → один ход. Ход — это `dialog/turn.py:run_turn()`.
Порядок фаз инвертирован против первоначального дизайна (живой баг: реплика
«отвожу вас к кафе» + действие `noop` в том же ходу) — реплика генерируется
ПОСЛЕ исполнения действия и видит его реальный итог:

```
транскрипт (ведущее wake-слово срезано; голое «робот» ход не запускает)
   │
   ├─ АВТОСПРАВКА (~/call_tool, ДО фазы действия, п.7.1)
   │     lookup_content(exhibit_id текущей остановки, mode=full) -- если есть
   │     search_content(query=реплика, max_results=5) -- всегда
   │     → блок СПРАВКА (целые чанки, ≤2500 символов, остановка первой)
   │
   ├─ ФАЗА ДЕЙСТВИЯ (GBNF, temperature.action)
   │     messages = [system] + history
   │                + [user: СОБЫТИЕ:*, [состояние: ...], СПРАВКА, реплика]
   │                + [user: action_instruction]
   │     → {"think": "...", "tool": "...", "args": {...}}
   │       (think — короткое явное рассуждение, ReAct-Thought; уезжает в jsonl)
   │
   ├─ исполнение через ~/call_tool (barge-in до исполнения — действие отменяется)
   │     ok:false → одна попытка починки (`llm.action_repair_attempts`),
   │     посетитель её не слышит — ничего ещё не сказано
   │     read_only (lookup_content/search_content/resolve_location) --
   │     итог фазе реплики целиком (chunks/hits), не "выполнено: name(...)"
   │
   ├─ ФАЗА РЕПЛИКИ (без грамматики, temperature.answer)
   │     messages += [assistant: tool-call JSON]
   │                + [user: answer_instruction + "Итог действия: ..."]
   │     → свободный русский текст, согласованный с реальным итогом
   │
   ├─ speak(text)  (barge-in до озвучки — реплика отбрасывается)
   │
   └─ history.append(visitor=utterance, robot=text если сказана,
                     event=итог действия -- read_only коротко: "уточнил справку: <title>")
```

`say` — больше не инструмент, видимый модели: реплика — результат фазы
реплики, а не выбор модели. Обработчик `say` в `tool_broker` остаётся (его
зовёт сам `dialog_agent`, `ToolSpec.llm_visible=False`). Каталог
инструментов не идёт в системный промпт — иначе модель зачитывала вслух
описания инструментов. Он рендерится в `action_instruction`
(`dialog/prompt.py:build_action_instruction()`); обе инструкции фаз
считаются один раз на `on_activate` и передаются в `run_turn()`
параметрами — обязаны быть побайтово одинаковыми на каждый ход (иначе
теряется `CACHE_REUSE` префикса). Справочники (`list_locations`/
`list_tours`/`estimate_route`) тоже скрыты от модели — локации и туры
едут в системном промпте, собранном один раз на `on_activate`.
Подробности и мотивация — `DIALOG_REWORK_PLAN.md` §0/§1,
`CLAUDE_CODE_TASK.md` пп.2/3/5.

## Топология

```
/asr/transcript ────┐
/mission/state ──────┼──►┌──────────────┐──► RunTour (action, mission_fsm)
/mission/presence ───┘   │              │──► ~/request_pause, ~/request_resume,
                          │ tool_broker  │     ~/submit_confirm, ~/submit_answer
             ~/call_tool  │              │     (mission_fsm)
              (srv) ◄─────┤              │──► ListLocations, ListTours,
                    │      └──────────────┘     EstimateRoute (semantic_map)
                    │                     └──► Say, Narrate (voice/mission_control)
                    │
/asr/transcript ────┼──┐
/mission/state ──────┼──┼──►┌────────────────┐
/mission/presence ───┘  │   │  dialog_agent  │──► HTTP /v1/chat/completions x2
/speech/cancel_all ─────┘   │ (ход: действие │     (llm_server/, вне ROS)
                             │   → реплика)   │
                             └────────┬───────┘
                              /dialog/interaction
                                       │
                             ┌─────────▼───────┐
                             │ interaction_log │──► jsonl на диск (схема v4)
                             └─────────────────┘
```

`tool_broker` и `dialog_agent` — разные процессы (`main()` каждого —
`rclpy.init` → один узел → `spin()`), поэтому `~/call_tool` — не
внутренний вызов, а реальный ROS-сервис: `dialog_agent` не может
дотянуться до Python-метода `ToolBrokerNode.call_tool()` напрямую.
`interaction_log` подписан на `dialog_agent` fire-and-forget — медленный
диск не блокирует ход/barge-in abort.

## Ноды

### `tool_broker`

Единственный держатель клиентов к mission/semantic_map/voice; валидация
+ гейт по состоянию (`tools/schema.py`/`tools/validate.py`) живут здесь,
не в FSM и не в промпте — попытка `start_tour` во время тура получает
внятный `ToolResult(ok=False, "тур уже идёт...")`, не `REJECT` от action-
сервера. `call_tool()` — единственная точка входа для любого вызывающего
(CLI-скрипт в тестах, `~/call_tool` для `dialog_agent`) — гарантирует
одинаковый гейт независимо от транспорта, включая `say`, который
`dialog_agent` зовёт напрямую (не через выбор модели).

**Сервис**: `~/call_tool` (`CallTool.srv`, `guide_robot_msgs`) — `name`
+ `args_json` (JSON, не нативный ROS-тип: `.srv` не знает generic
map/dict) → `ok`/`message`/`data_json`.

**Действия**: `RunTour` (не ждёт результата — только принятия goal-а:
рассказ на 3 минуты не должен вешать ход), `Say`, `Narrate` (оба тоже
fire-and-forget — см. «Известные пробелы» про `content_version`).

**Клиенты-сервисы**: `~/request_pause`, `~/request_resume`
(`std_srvs/Trigger`), `~/submit_confirm` (`std_srvs/SetBool`),
`~/submit_answer` (`SubmitAnswer.srv`) — все на `mission_fsm`.
Read-only: `~/list_locations`, `~/list_tours`, `~/estimate_route` на
`location_server`/`route_planner` (whitelist локаций/туров кэшируется
ОДИН раз на `on_activate`, не на каждый `call_tool()`, сбрасывается на
`on_deactivate`); `~/get_exhibit_content`, `~/search_content` на
`content_server` и `~/resolve_location` на `location_server`
(`content_server_ns`/`location_server_ns`, п.6) — видны модели как
`lookup_content`/`search_content`/`resolve_location`, буст `search_content`
берёт `stop_id` из кэша `/mission/state`.

**Подписки**: `/mission/state`, `/mission/presence` (свой кэш),
`/asr/transcript` — быстрый путь мимо ЛЛМ: `matching.py` разбирает
да/нет (`AWAITING_CONFIRM`) и стоп-фразы (`ANSWERING`) локально по
финалам ASR и сразу зовёт `~/submit_confirm`/`~/submit_answer`, не ждёт
ЛЛМ.

**Параметры**: `service_call_timeout_s`(2.0), `mission_fsm_ns`
(`/mission_fsm`), `location_server_ns` (`/location_server`),
`route_planner_ns` (`/route_planner`).

### `dialog_agent`

Ход «действие → реплика» (см. выше): транскрипт → снимок состояния →
фаза действия (GBNF, форма `{"think":..,"tool":..,"args":{...}}`) →
`~/call_tool` → фаза реплики (свободный текст, знает итог действия) →
`speak()` → история. Провалившийся вызов действия (`ok:false`) не
заканчивает ход молча — одна попытка починки
(`llm.action_repair_attempts`) ДО реплики, посетитель её не замечает.
Транскрипт, пришедший пока ход в полёте, не выбрасывается: текущий ход
прерывается (семантика barge-in), реплика ждёт в однослотовой очереди и
отыгрывается сразу после (последняя побеждает).

Кэш `/mission/state`/`/mission/presence` — свой, не `tool_broker`-овский
(разные процессы). На каждый финальный транскрипт сначала прогоняется
тот же `matching.py`-чек, что у `tool_broker` — уверенный матч означает
«`tool_broker` уже обработал сам», ЛЛМ не зовём; вместо этого в историю
дописывается, ЧТО было распознано (не что сделал `tool_broker` — агент
этого не наблюдает).

**Каталог**: локации/туры тянутся через `~/call_tool` ОДИН раз на
`on_activate` и рендерятся в системный промпт (`dialog/prompt.py`) —
координаты в промпт не идут. Если каталог не пришёл за
`catalog_ns_timeout_s` — `on_activate` возвращает `FAILURE` (агент без
каталога не может назвать ни одной локации).

**Память диалога** (`dialog/history.py`): append-only, режется по
символам при записи (без токенизатора), обрезка половинами при
превышении `history.max_entries`. Очищается: (1) если посетитель
отсутствует дольше `history.clear_after_absent_s`, (2) на
`on_deactivate`/`on_cleanup`. Переход в `IDLE` по концу тура сам по себе
историю больше НЕ чистит (`CLAUDE_CODE_TASK.md` п.4, живой баг: тур
остановлен, посетитель продолжает говорить про него, а история уже
стёрта) — переходы `/mission/state` по-прежнему дописываются в историю
как события (переход состояния, приход на остановку — с
`told_ids.add()`, начало/прерывание вопроса, начало/конец тура) — только
на ИЗМЕНЕНИЕ поля, без дребезга от heartbeat.

**Корпус знаний убран** (`CLAUDE_CODE_TASK_stage1_knowledge.md` п.5):
локального `kb/` (пассажи из `.md`, `config/kb.jsonl`) в пакете больше
нет. Единственный источник фактов про экспонаты/площадку/город —
`guide_robot_semantic_map/content/`; `dialog_agent` читает его за ход
через read-only инструменты, не встраивает целиком в системный промпт.
Пустой результат поиска — не ошибка: модель честно говорит «не знаю»
(правило грунтования в `config/system_prompt.txt`).

**Barge-in** (`/speech/cancel_all`, `REASON_BARGE_IN`): взводит
`abort_event`, `llm_client.Backend` ловит его между SSE-чанками и
поднимает `BackendAborted` в любой из двух фаз. Дополнительно
`run_turn()` проверяет `abort_event` РОВНО один раз между фазой 1 и
`speak()` — если посетитель отменил, пока текст ещё генерировался,
начинать говорить уже нельзя. Один ход в полёте максимум.

**Публикует**: `/dialog/interaction` (`InteractionEvent`, fire-and-forget,
для `interaction_log`).

**Параметры**: `llm.base_urls`, `llm.connect_timeout_s`(2.0),
`llm.read_timeout_s`(30.0), `llm.api_key`(""),
`llm.max_attempts_per_backend`(2), `llm.backoff_s`(0.5),
`llm.max_tokens_answer`(160), `llm.max_tokens_action`(128),
`llm.temperature_answer`(0.6), `llm.temperature_action`(0.0),
`llm.action_repair_attempts`(1), `system_prompt_path`,
`tool_broker_ns`(`/tool_broker`), `service_call_timeout_s`(2.0),
`catalog_ns_timeout_s`(5.0), `history.max_entries`(16),
`history.trim_to`(8), `history.cap_visitor_chars`(200),
`history.cap_robot_chars`(300), `history.cap_event_chars`(120),
`history.clear_after_absent_s`(90.0 в `config/llm.yaml`, 25.0 если
параметр не задан — `presence_monitor` выводит присутствие из речевой
активности, короткая пауза в разговоре не должна читаться как уход
посетителя), `answer.max_chars`(400), `wake_grace_s`(30.0 — окно после
конца хода, в течение которого транскрипты в `IDLE` принимаются без
wake-слова; сбрасывается каждым ходом, обнуляется по `presence=false`).

### `interaction_log`

jsonl-sink: одна строка на ход (`InteractionSink`, flush на каждую
запись). Подписан на `/dialog/interaction`; битый `payload_json` — лог
ошибки, не падение ноды.

**Параметры**: `log_dir` (`~/.guide_robot/llm_turns`) — файл
`interaction_YYYYmmdd_HHMMSS.jsonl` на сессию активации.

**Формат записи** (схема v5, `dialog/interaction_log.py`):

```json
{
  "schema_version": 5,
  "ts": 1730000000.123, "turn_id": 42,
  "session_id": "3f9a1c2b4d5e", "utterance_ts": 1730000000.001,
  "mission_state": "NARRATING",
  "utterance": "а что это за штука?",
  "snapshot": {"...": "то, что ушло бы в промпт (для лога)"},
  "references": [{"content_id": "robo_guide", "chunk_id": "c5",
                  "score": 0.0, "source": "auto"}],
  "answer_text": "Это макет университетского кампуса...",
  "answer_chars": 96,
  "answer_raw_text": "Это макет университетского кампуса...",
  "answer_finish_reason": "stop",
  "action_raw_text": "{\"think\": \"...\", \"tool\": \"noop\", \"args\": {}}",
  "action_finish_reason": "stop",
  "verbatim_overlap_words": 3,
  "say_ok": true,
  "action": {"tool": "noop", "args": {}, "think": "светская реплика",
             "ok": true, "message": "", "content_version": null},
  "repair_used": false,
  "history_entries": 9,
  "history_cleared": false,
  "told_ids": ["lab_demo"],
  "stage_timings": [{"stage": "llm_action", "ms": 480.2},
                    {"stage": "llm_answer", "ms": 2100.4},
                    {"stage": "say", "ms": 11.0}],
  "stopped_reason": "ok", "degraded": false, "degrade_reason": null,
  "total_ms": 2595.1,
  "llm_messages": [{"role": "system", "content": "..."}, "..."]
}
```

`(session_id, turn_id)` глобально уникальна — `turn_id` сам по себе лишь
процессный счётчик, перезапуск `dialog_agent` при живом `interaction_log`
начинает его заново. `action.think` — явное рассуждение модели перед
выбором инструмента (готовая диагностика «почему выбрана эта ветка»).
`llm_messages` — `TurnResult.messages` как есть: весь обмен с ЛЛМ за ход
(system prompt, история, реплика посетителя, сырой tool-call на каждой
попытке починки, инструкция и сырой текст фазы реплики) — единственное
место, где виден буквально весь ввод/вывод модели. `answer_raw_text`/`action_raw_text` —
то же самое отдельными полями, ДО постобработки: `answer_text` уже прошёл
`sanitize_answer` (markdown/самопредставление/обрезка), а `answer_raw_text`
— то, что модель ответила буквально. `action_raw_text`/`action_finish_reason`
заполнены и когда `action` — `null` (`stopped_reason=action_parse_error`):
единственное место, где виден сырой (невалидный) tool-call модели в этом
случае. `*_finish_reason` — как сервер объяснил остановку генерации
(`stop`/`length`/...), пусто, если бэкенд вообще не ответил.

`content_version` — версия из `result_data["version"]`, если read_only-вызов
её вернул (`lookup_content`/`search_content` синхронны); `null` для
остальных инструментов — `tool_broker._tool_tell_about`/`_tool_say` не
ждут результата `Narrate`/`Say` (fire-and-forget), версия реально
озвученного контента до `dialog_agent` не доходит.

`references` — все чанки `guide_robot_semantic_map/content/`, что модель
видела в ходу: автосправка перед фазой действия (`source: "auto"`,
`dialog_agent_node.py::_run_turn`, CLAUDE_CODE_TASK_stage1_knowledge.md
п.7.1) + явный read_only-вызов, если модель его выбрала (`source: "tool"`,
п.7.2/7.3). Пусто — за ход не нашли ничего ни автосправкой, ни вызовом.
`verbatim_overlap_words` (`dialog/verbatim.py`) — длина самой длинной общей
последовательности слов между ответом и `corpus_texts` (тексты этих же
чанков, без метаданных); >= 8 — модель, вероятно, цитирует дословно, а не
пересказывает. Метрика per-turn (не против всего корпуса — локального
корпуса знаний больше нет, п.5).

## Каталог инструментов (`tools/schema.py`)

Гейт «какие инструменты сейчас разрешены» — таблица `ToolSpec.allowed_states`
по `MissionState.state`, один источник для `tool_broker.call_tool()`
(`llm_only=False`, гейт по состоянию для всех вызывающих) и для
GBNF-каталога/`tools_allowed` в снимке (`llm_only=True`, дополнительно
фильтрует по `ToolSpec.llm_visible`).

| Инструмент | Реальный вызов | Гейт | `llm_visible` | `read_only` |
|---|---|---|---|---|
| `start_tour` | `RunTour(tour_id)` | `IDLE` | да | нет |
| `guide_to` | `RunTour(location_ids=[id])` в `IDLE`, `~/redirect` вне (stage2 B3) | любое | да | нет |
| `tour_by_points` | `EstimateRoute` → `RunTour(location_ids=ordered)` | `IDLE` | да | нет |
| `stop_tour` | отмена активного `RunTour`-goal-а | любое, кроме `IDLE` | да | нет |
| `pause` / `resume` | `~/request_pause` / `~/request_resume` | `NARRATING` / `PAUSED` | да | нет |
| `confirm` | `~/submit_confirm` | `AWAITING_CONFIRM` | да | нет |
| `finish_answer` | `~/submit_answer` | `ANSWERING` | да | нет |
| `noop` | ничего | любое | да | нет |
| `say` | `Say`, `PRIORITY_DIALOG`/`SCOPE_DIALOG` | любое | **нет** — зовёт сам `dialog_agent` | нет |
| `tell_about` | `Narrate` | только `IDLE` (вне тура) | да | нет |
| `lookup_content` | `GetExhibitContent(exhibit_id=content_id)` | любое | да | **да** |
| `search_content` | `SearchContent(query, boost=stop_id)` | любое | да | **да** |
| `resolve_location` | `ResolveLocation(query)` | любое | да | **да** |
| `list_locations` / `list_tours` / `estimate_route` | read-only, `semantic_map` | любое | **нет** — каталог в промпте | **да** |

## Чистая логика без ROS

Тестируется без поднятого rclpy и без HTTP, отдельно от узлов —
конвенция пакета: узел только раскладывает ROS-msg/HTTP-ответ по полям
чистых функций.

| Модуль | Что делает |
|---|---|
| `tools/schema.py` | Каталог инструментов + таблица гейтов по состоянию/`llm_visible` |
| `tools/validate.py` | Валидация args (whitelist локаций/туров, форма) до похода в ROS |
| `matching.py` | ASR-фраза → да/нет/стоп-слово, локально, без ЛЛМ (с гейтом по длине/вопросам) |
| `snapshot.py` | `MissionState`+`Presence` → компактный dict для промпта |
| `llm_client/backend.py` | Один HTTP-бэкенд, всегда стримит (нужно для abort) |
| `llm_client/grammar.py` | GBNF по форме tool-call JSON, не по содержимому |
| `llm_client/ladder.py` | Список бэкендов, retry/backoff, без stateful circuit breaker |
| `dialog/history.py` | Память диалога между ходами: append-only, обрезка по символам/половинам |
| `dialog/sanitize.py` | Санитайзер фазы реплики: markdown/самопредставление/tool-call JSON (в т.ч. приклеенный к тексту)/обрезка по границе предложения |
| `dialog/turn.py` | Двухфазный ход: действие → исполнение → реплика → `speak()`, с починкой; read_only-итог рендерится фазе реплики целиком |
| `dialog/prompt.py` | Системный промпт (преамбул + каталог локаций/туров) и инструкции фаз (`build_action_instruction`: каталог инструментов + правила выбора; `build_answer_instruction`: правила реплики) |
| `dialog/interaction_log.py` | Сборка одной jsonl-записи хода (схема v4) |
| `dialog/verbatim.py` | Длина самой длинной общей последовательности слов (метрика цитирования) |
| `lib/interaction_sink.py` | Построчный jsonl, flush на запись |

`lib/qos.py` — единственный модуль пакета, которому разрешено
импортировать `rclpy` из «чистых» модулей верхнего уровня.

## Интерфейсы (сводно)

| Интерфейс | Тип | Нода |
|---|---|---|
| `~/call_tool` | `CallTool` (srv) | tool_broker (сервер), dialog_agent (клиент) |
| `/dialog/interaction` | `InteractionEvent` (pub, RELIABLE/VOLATILE depth 10) | dialog_agent → interaction_log |
| `/mission/state`, `/mission/presence` | `MissionState`/`Presence` (sub, TRANSIENT_LOCAL) | tool_broker, dialog_agent (независимо) |
| `/asr/transcript` | `Transcript` (sub, RELIABLE depth 10) | tool_broker, dialog_agent (независимо) |
| `/speech/cancel_all` | `CancelAll` (sub, RELIABLE/VOLATILE) | dialog_agent (abort хода) |

QoS-профили — `lib/qos.py`.

## Запуск

```bash
# Все три ноды, unconfigured -- подъём вручную или через supervisor
ros2 launch guide_robot_llm llm.launch.py

# Автоподъём в порядке tool_broker -> dialog_agent -> interaction_log
ros2 launch guide_robot_llm llm.launch.py autostart:=true
```

Ручной подъём (`autostart:=false`):

```bash
ros2 lifecycle set /tool_broker configure && ros2 lifecycle set /tool_broker activate
ros2 lifecycle set /dialog_agent configure && ros2 lifecycle set /dialog_agent activate
ros2 lifecycle set /interaction_log configure && ros2 lifecycle set /interaction_log activate
```

Перед `dialog_agent`: `llm_server/` должен отвечать на `/health` (см.
`../llm_server/README.md`) — иначе каждый ход уходит в
`degrade_reason=backend_error`/`answer_backend_error` после исчерпания
`llm.max_attempts_per_backend`. `dialog_agent.on_activate` также требует
живого `tool_broker` (каталог локаций/туров) — активировать `tool_broker`
раньше.

Не зарегистрирован в `guide_robot_supervisor` — по прецеденту с `voice`/
`semantic_map` (см. `guide_robot_mission_control/README.md`, «Известные
грабли»), регистрация отложена до ручной проверки живого стека.

## Известные пробелы

- **`content_version` в `interaction_log` -- `null` для `tell_about`/`say`.**
  `tool_broker` не ждёт результата `Say`/`Narrate` (fire-and-forget по
  дизайну), поэтому версия реально озвученного контента (`GetExhibitContent`)
  никогда не доходит обратно до `dialog_agent`. Тот же корень, что у
  `truncated` в истории — приближение через `CancelAll`, не точное значение.
  Для `lookup_content`/`search_content` (синхронные read_only-вызовы)
  версия заполняется реально, см. `dialog/interaction_log.py`.
- **`nearby` в снимке не заполняется.** `snapshot.build_snapshot()`
  поддерживает параметр (id ближайших локаций по координатам), но
  `dialog_agent` не подписан ни на одну публикацию текущей позы робота —
  посчитать «рядом» не из чего. Осознанный пробел этого захода, не
  тихий пропуск.
- **Мид-тур переадресация реализована для `guide_to`, не для
  `tour_by_points`** (stage2 B3): «отведи меня к X» во время тура
  маппится на `guide_robot_mission_control`'s `~/redirect`, а не на
  `RunTour`. `tour_by_points` по-прежнему только `IDLE` — составной
  маршрут посреди тура не заявлен в спеке.
- **Whitelist локаций/туров в `tool_broker` кэшируется один раз на
  `on_activate`.** Локация/тур, добавленные в `location_server` ПОСЛЕ
  активации `tool_broker`, не пройдут валидацию до следующей
  реактивации — осознанный компромисс латентности (план §7.2).
- **FSM-таймаут `answer_max_s` не детектируется как отдельная
  деградационная метрика.** Если `dialog_agent` не успел ответить,
  `mission_fsm` резюмирует сам — деградация корректная, но не помечена в
  `interaction_log` отдельно от обычного успешного хода.
- **GBNF проверяется только структурно.** В тестовом окружении нет
  `llama.cpp`-бинаря для реального разбора грамматики (его поднимает
  `llm_server/`) — `test_llm_client_grammar.py` проверяет форму
  сгенерированного текста, не то, что `llama-server` действительно
  примет его как валидный GBNF.
- **`python3 -m pytest test -q` без флага падает.** `anyio` (pip, 4.x)
  не совместим с системным `pytest` 6.2.5 в образе контейнера — нужен
  `-p no:anyio`. Пре-существующий, общеконтейнерный дефект.
- **Не смокано против реального `llm_server`.** Все тесты — на
  `MockLlmServer` (голый `http.server`, различает фазы по наличию
  `grammar` в теле запроса). Живой прогон (`scripts/eval_turns.py`
  против настоящего `llama.cpp`) не выполнялся из этого контейнера.
- **`say` ack'ается по ПРИНЯТИЮ цели, не по концу озвучки.** Ответ на
  реплику N может звучать заметно позже конца хода N; отложенная реплика
  из однослотовой очереди отыгрывается сразу после хода, не дожидаясь
  конца звука. Completion-aware `say` (ожидание результата `Say` или
  подписка на состояние голосового планировщика) — отдельный заход.
- **Два независимых кэша `/mission/state`.** `dialog_agent` фиксирует
  состояние в момент транскрипта (по нему строится GBNF-каталог),
  `tool_broker` перегейтивает своим кэшем в момент исполнения — переход
  состояния между двумя вызовами ЛЛМ может сделать действие легальным
  для грамматики и нелегальным для брокера (ход честно закончится
  `action_invalid` с репликой-извинением, но не выполнит намерение).
- **Блуждающий флак полного прогона тестов.** На `pytest test -q`
  целиком изредка падает один из harness-тестов `test_voice_confirm`/
  `test_tool_gating` (гонки teardown DDS-графов между последовательными
  harness'ами: `Goal state not set`, invalid feedback publisher); в
  изоляции и в малых батчах — стабильно зелёные.

## Тесты

```bash
cd guide_robot_llm
python3 -m pytest test -q -p no:anyio
ruff check .
```

Без ROS-железа — rclpy + моки (`test/mocks/`: `mock_llm_server.py` —
голый `http.server`, chunked SSE, различает фазы по `grammar` в теле
запроса; `mock_nav_server.py`/`mock_say_server.py`/`mock_semantic_map.py`/
`sim_clock.py` — переиспользованы из `guide_robot_mission_control` тем
же приёмом «копия, не импорт»). `test/mocks/harness.py` поднимает
РЕАЛЬНЫЕ `mission_fsm`/`narration_server` (не мок поверх мока) +
`tool_broker`+`dialog_agent`+`interaction_log` в одном `rclpy.Context()`.

| Файл | Что проверяет |
|---|---|
| `test_schema.py`, `test_validate.py` | Каталог инструментов, гейты, `llm_visible`/`llm_only`, валидация args |
| `test_matching.py` | ASR-фраза → да/нет/стоп-слово, гейт по длине/вопросительным словам |
| `test_snapshot.py` | Сборка компактного dict для промпта, включая `already_told`/`nearby` |
| `test_history.py` | Память диалога: обрезка при записи, склейка событий, обрезка половинами |
| `test_sanitize.py` | Санитайзер фазы реплики: markdown, самопредставление, tool-call JSON (хвост/начало/середина), граница предложения |
| `test_turn.py` | Двухфазный ход на фейковых `complete_*`/`speak`/`execute_tool`, read_only-рендер итога (`chunks`/`hits`/`candidates`) |
| `test_verbatim.py` | Метрика самой длинной общей последовательности слов |
| `test_llm_client_backend.py` | HTTP-механика: stream, timeout, HTTP-ошибка, abort |
| `test_llm_client_ladder.py` | Порядок бэкендов, retry, abort не ретраится |
| `test_llm_client_grammar.py` | GBNF форма (не содержимое) |
| `test_dialog_prompt.py` | Сборка системного промпта (каталог локаций/туров, детерминизм), `build_action_instruction` (каталог инструментов, honest noop, последняя реплика) и `build_answer_instruction` |
| `test_interaction_sink.py` | jsonl-sink: flush, newline-delimited, idempotent close |
| `test_interaction_log.py` | Сборка jsonl-записи схемы v5 из `TurnResult`, включая сырой ввод/вывод ЛЛМ (`llm_messages`, `*_raw_text`, `*_finish_reason`) и `references` (`content_id`/`chunk_id`/`score`/`source`) |
| `test_tool_gating.py` | Полный тур/пауза/стоп/barge-in/`noop`/кэш whitelist ТОЛЬКО через `call_tool()` |
| `test_voice_confirm.py` | `AWAITING_CONFIRM`/`ANSWERING` закрываются голосом мимо ЛЛМ |
| `test_dialog_agent_e2e.py` | Транскрипт → ход «действие → реплика» (мок) → `~/call_tool`, barge-in abort, очередь транскриптов, fast-path, wake-слово, события истории, автосправка в `user_content` + `references` в логе |
| `test_interaction_log_e2e.py` | Ход через `dialog_agent` → jsonl-запись схемы v5 на диске |
| `test_answering_closes.py` | Регресс: в `ANSWERING` ход не может выбрать `say` как действие |

`scripts/eval_turns.py`/`scripts/extract_golden.py` — не тесты в CI,
ручные скрипты для прогона golden-набора против живого `llm_server`
(`DIALOG_REWORK_PLAN.md` §9).
