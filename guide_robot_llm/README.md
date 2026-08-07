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
реализации — см. также `llm_plam.md` (план по шагам) и раздел «Отличия»
ниже про то, где реализация от него разошлась.

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
/mission/presence ───┘  │   │  dialog_agent  │──► HTTP /v1/chat/completions
/speech/cancel_all ─────┘   │  (ReAct-цикл)   │     (llm_server/, вне ROS)
                             └────────┬───────┘
                              /dialog/interaction
                                       │
                             ┌─────────▼───────┐
                             │ interaction_log │──► jsonl на диск
                             └─────────────────┘
```

`tool_broker` и `dialog_agent` — разные процессы (`main()` каждого —
`rclpy.init` → один узел → `spin()`), поэтому `~/call_tool` — не
внутренний вызов, а реальный ROS-сервис: `dialog_agent` не может
дотянуться до Python-метода `ToolBrokerNode.call_tool()` напрямую.
`interaction_log` подписан на `dialog_agent` fire-and-forget — медленный
диск не блокирует ReAct-цикл/barge-in abort.

## Ноды

### `tool_broker`

Единственный держатель клиентов к mission/semantic_map/voice; валидация
+ гейт по состоянию (`tools/schema.py`/`tools/validate.py`) живут здесь,
не в FSM и не в промпте — попытка `start_tour` во время тура получает
внятный `ToolResult(ok=False, "тур уже идёт...")`, не `REJECT` от action-
сервера. `call_tool()` — единственная точка входа для любого вызывающего
(CLI-скрипт в тестах, `~/call_tool` для `dialog_agent`) — гарантирует
одинаковый гейт независимо от транспорта.

**Сервис**: `~/call_tool` (`CallTool.srv`, `guide_robot_msgs`) — `name`
+ `args_json` (JSON, не нативный ROS-тип: `.srv` не знает generic
map/dict) → `ok`/`message`/`data_json`.

**Действия**: `RunTour` (не ждёт результата — только принятия goal-а:
рассказ на 3 минуты не должен вешать ReAct-ход), `Say`, `Narrate` (оба
тоже fire-and-forget — см. «Известные пробелы» про `content_version`).

**Клиенты-сервисы**: `~/request_pause`, `~/request_resume`
(`std_srvs/Trigger`), `~/submit_confirm` (`std_srvs/SetBool`),
`~/submit_answer` (`SubmitAnswer.srv`) — все на `mission_fsm`.
Read-only: `~/list_locations`, `~/list_tours`, `~/estimate_route` на
`semantic_map`.

**Подписки**: `/mission/state`, `/mission/presence` (свой кэш),
`/asr/transcript` — быстрый путь мимо ЛЛМ: `matching.py` разбирает
да/нет (`AWAITING_CONFIRM`) и стоп-фразы (`ANSWERING`) локально по
финалам ASR и сразу зовёт `~/submit_confirm`/`~/submit_answer`, не ждёт
ЛЛМ (риск §9 плана — суммарная латентность ASR→ЛЛМ→сервис не должна
решать судьбу простого «да»).

**Параметры**: `service_call_timeout_s`(2.0), `mission_fsm_ns`
(`/mission_fsm`), `location_server_ns` (`/location_server`),
`route_planner_ns` (`/route_planner`).

### `dialog_agent`

ReAct-цикл: транскрипт → снимок состояния → `complete()` с GBNF-
грамматикой (только форма `{"tool":..,"args":{...}}`, не типизация
per-tool — семантику по-прежнему проверяет `tool_broker`) → распарсенный
tool-call → `~/call_tool` → результат обратно в диалог → повтор, до
`max_tool_calls_per_turn`(2) или до терминального инструмента (`say`/
`confirm`/`stop_tour`/... — список в `dialog/loop.py`; read-only
справочники терминальными не считаются).

Кэш `/mission/state`/`/mission/presence` — свой, не `tool_broker`-овский
(разные процессы). На каждый финальный транскрипт сначала прогоняется
тот же `matching.py`-чек, что у `tool_broker` — уверенный матч означает
«`tool_broker` уже обработал сам», ЛЛМ не зовём (иначе второй,
потенциально противоречащий tool-call на ту же реплику).

**Barge-in** (`/speech/cancel_all`, `REASON_BARGE_IN`): взводит
`abort_event`, `llm_client.Backend` ловит его между SSE-чанками и
поднимает `BackendAborted` — частичный ответ отбрасывается, `execute_tool`
для оборванного шага не зовётся. Один ход в полёте максимум — новый
транскрипт, пока предыдущий ход не завершился/не оборвался, отбрасывается
(лог, не очередь — осознанное упрощение).

**Публикует**: `/dialog/interaction` (`InteractionEvent`, fire-and-forget,
для `interaction_log`).

**Параметры**: `llm.base_urls` (список, пробуются по порядку с retry —
не stateful circuit breaker, см. `llm_client/ladder.py`),
`llm.connect_timeout_s`(2.0), `llm.read_timeout_s`(30.0),
`llm.max_tokens`(512), `llm.temperature`(0.2),
`llm.max_attempts_per_backend`(2), `llm.backoff_s`(0.5), `llm.api_key`
(""), `system_prompt_path` (файл, не embedded-текст — та же копия
греет `llm_server/config/system_prompt.txt`), `tool_broker_ns`
(`/tool_broker`), `service_call_timeout_s`(2.0),
`max_tool_calls_per_turn`(2).

### `interaction_log`

jsonl-sink: одна строка на ход (`InteractionSink`, flush на каждую
запись — падение процесса не должно стоить последних строк). Подписан
на `/dialog/interaction`; битый `payload_json` — лог ошибки, не падение
ноды (защита от бага на стороне `dialog_agent`, не повод ронять sink).

**Параметры**: `log_dir` (`~/.guide_robot/llm_turns`) — файл
`interaction_YYYYmmdd_HHMMSS.jsonl` на сессию активации.

**Формат записи**:

```json
{
  "ts": 1730000000.123, "turn_id": 42, "mission_state": "IDLE",
  "utterance": "привет", "snapshot": {"...": "то, что ушло в промпт"},
  "calls": [{"tool": "say", "args": {"text": "..."}, "ok": true,
             "message": "", "content_version": null}],
  "stage_timings": [{"stage": "llm_call", "tool": null, "ms": 812.3},
                     {"stage": "tool_call", "tool": "say", "ms": 12.1}],
  "stopped_reason": "terminal_tool", "degraded": false,
  "degrade_reason": null, "total_ms": 850.2
}
```

`content_version` всегда `null` — известный пробел, см. «Известные
пробелы». `degrade_reason` — `"aborted"`/`"backend_error"`/`null`;
FSM-таймаут `answer_max_s` отдельно НЕ детектируется (см. там же).

## Каталог инструментов (`tools/schema.py`)

Гейт «какие инструменты сейчас разрешены» — таблица `ToolSpec.allowed_states`
по `MissionState.state`, один источник для `tool_broker.call_tool()` и
для снимка (`tools_allowed` в промпте).

| Инструмент | Реальный вызов | Гейт |
|---|---|---|
| `start_tour` | `RunTour(tour_id)` | `IDLE` |
| `guide_to` | `RunTour(location_ids=[id])` | `IDLE` |
| `tour_by_points` | `EstimateRoute` → `RunTour(location_ids=ordered)` | `IDLE` |
| `stop_tour` | отмена активного `RunTour`-goal-а | любое, кроме `IDLE` |
| `pause` / `resume` | `~/request_pause` / `~/request_resume` | `NARRATING` / `PAUSED` |
| `confirm` | `~/submit_confirm` | `AWAITING_CONFIRM` |
| `finish_answer` | `~/submit_answer` | `ANSWERING` |
| `say` | `Say`, `PRIORITY_DIALOG`/`SCOPE_DIALOG` | любое |
| `tell_about` | `Narrate` | только `IDLE` (вне тура) |
| `list_locations` / `list_tours` / `estimate_route` | read-only, `semantic_map` | любое |

## Чистая логика без ROS

Тестируется без поднятого rclpy и без HTTP, отдельно от узлов —
конвенция пакета: узел только раскладывает ROS-msg/HTTP-ответ по полям
чистых функций.

| Модуль | Что делает |
|---|---|
| `tools/schema.py` | Каталог инструментов + таблица гейтов по состоянию |
| `tools/validate.py` | Валидация args (whitelist локаций/туров, форма) до похода в ROS |
| `matching.py` | ASR-фраза → да/нет/стоп-слово, локально, без ЛЛМ |
| `snapshot.py` | `MissionState`+`Presence` → компактный dict для промпта |
| `llm_client/backend.py` | Один HTTP-бэкенд, всегда стримит (нужно для abort) |
| `llm_client/grammar.py` | GBNF по форме tool-call JSON, не по содержимому |
| `llm_client/ladder.py` | Список бэкендов, retry/backoff, без stateful circuit breaker |
| `dialog/loop.py` | ReAct-шаг: сообщения → tool call → результат → сообщения |
| `dialog/prompt.py` | Системный промпт: преамбул (файл) + каталог инструментов |
| `dialog/interaction_log.py` | Сборка одной jsonl-записи из `ReactTurnResult`+тайминга |
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
`degrade_reason=backend_error` после исчерпания `llm.max_attempts_per_backend`.

Не зарегистрирован в `guide_robot_supervisor` — по прецеденту с `voice`/
`semantic_map` (см. `guide_robot_mission_control/README.md`, «Известные
грабли»), регистрация отложена до ручной проверки живого стека.

## Известные пробелы

- **`content_version` в `interaction_log` всегда `null`.** `tool_broker`
  не ждёт результата `Say`/`Narrate` (fire-and-forget по дизайну — long-
  running действие не должно вешать ReAct-ход), поэтому версия реально
  озвученного контента (`GetExhibitContent`) никогда не доходит обратно
  до `dialog_agent`. Прокинуть её означало бы менять fire-and-forget
  дизайн `_tool_say`/`_tool_tell_about` — отдельная доработка.
- **FSM-таймаут `answer_max_s` не детектируется как отдельная
  деградационная метрика** (риск §6 плана). Если `dialog_agent` не
  успел ответить, `mission_fsm` резюмирует сам — деградация корректная,
  но `dialog_agent` не наблюдает внутренний таймер `AnsweringState`
  напрямую и не помечает эту ситуацию в `interaction_log` отдельно от
  обычного успешного хода.
- **GBNF проверяется только структурно.** В тестовом окружении нет
  `llama.cpp`-бинаря для реального разбора грамматики (его поднимает
  `llm_server/`) — `test_llm_client_grammar.py` проверяет форму
  сгенерированного текста, не то, что `llama-server` действительно
  примет его как валидный GBNF.
- **Нет персистентной истории между РАЗНЫМИ репликами посетителя.**
  `dialog/loop.py` копит сообщения только внутри одного хода (между его
  же tool-call/результат парами); снимок несёт актуальное состояние
  каждый ход, длинная память не специфицирована ни в `llm_plam.md`, ни
  в design.
- **Один ход в полёте максимум.** Новый финальный транскрипт, пока
  предыдущий ход `dialog_agent` не завершился/не оборван abort-ом,
  просто пропускается (лог `"ход уже в полёте"`) — не встаёт в очередь.
- **`python3 -m pytest test -q` без флага падает.** `anyio`
  (pip, 4.x) не совместим с системным `pytest` 6.2.5 в образе
  контейнера (`_pytest.scope` появился только в pytest 7+) — нужен
  `-p no:anyio`. Пре-существующий, общеконтейнерный дефект, не
  специфичный для этого пакета (воспроизводится в любом пакете
  монорепо); фикс (`pytest.ini`) не сделан.
- **Не смокано против реального `llm_server`.** Все тесты — на
  `MockLlmServer` (`test/mocks/mock_llm_server.py`, голый `http.server`).
  Живой прогон с настоящим `llama.cpp` (шаг 7 плана) не выполнялся из
  этого контейнера.

## Тесты

```bash
cd guide_robot_llm
python3 -m pytest test -q -p no:anyio
ruff check .
```

96 тестов (95 проходят + 1 skip), без ROS-железа — rclpy + моки
(`test/mocks/`: `mock_llm_server.py` — голый `http.server`, chunked SSE;
`mock_nav_server.py`/`mock_say_server.py`/`mock_semantic_map.py`/
`sim_clock.py` — переиспользованы из `guide_robot_mission_control` тем
же приёмом «копия, не импорт», что и остальные пакеты монорепо).
`test/mocks/harness.py` поднимает РЕАЛЬНЫЕ `mission_fsm`/
`narration_server` (не мок поверх мока) + `tool_broker`+`dialog_agent`+
`interaction_log` в одном `rclpy.Context()`.

| Файл | Что проверяет |
|---|---|
| `test_schema.py`, `test_validate.py` | Каталог инструментов, гейты, валидация args |
| `test_matching.py` | ASR-фраза → да/нет/стоп-слово |
| `test_snapshot.py` | Сборка компактного dict для промпта |
| `test_llm_client_backend.py` | HTTP-механика: stream, timeout, HTTP-ошибка, abort |
| `test_llm_client_ladder.py` | Порядок бэкендов, retry, abort не ретраится |
| `test_llm_client_grammar.py` | GBNF форма (не содержимое) |
| `test_dialog_loop.py` | ReAct-шаг на фейковых complete/execute_tool |
| `test_dialog_prompt.py` | Сборка системного промпта |
| `test_interaction_sink.py` | jsonl-sink: flush, newline-delimited, idempotent close |
| `test_interaction_log.py` | Сборка jsonl-записи из `ReactTurnResult` |
| `test_tool_gating.py` | Полный тур/пауза/стоп/barge-in ТОЛЬКО через `call_tool()` |
| `test_voice_confirm.py` | `AWAITING_CONFIRM`/`ANSWERING` закрываются голосом мимо ЛЛМ |
| `test_dialog_agent_e2e.py` | Транскрипт → ЛЛМ (мок) → `~/call_tool`, barge-in abort, fast-path подавление |
| `test_interaction_log_e2e.py` | Ход через `dialog_agent` → jsonl-запись на диске |

Известный флейк, не специфичный для этого пакета: полный прогон
`guide_robot_mission_control`/`guide_robot_llm` подряд несколько раз под
нагрузкой хоста иногда даёт таймаут в barge-in→`ANSWERING`-сценариях
(фиксированные 5-секундные `wait_until` в тестах, воспроизводится даже
при полностью отключённом коде этого пакета) — не логическая ошибка,
одиночные прогоны стабильно зелёные.

## Отличия от `llm_plam.md`

1. **`~/call_tool` — сервис, не было в исходном плане явно.**
   `llm_plam.md` §3 называет `call_tool()` "единственной точкой входа
   для скриптов/dialog_agent", что при чтении можно принять за прямой
   Python-вызов. По факту `tool_broker`/`dialog_agent` — разные процессы
   (как и везде в монорепо, mission_fsm/narration_server тому пример) —
   единственная точка входа означает одну ТОЧКУ ГЕЙТА, не общий процесс.
2. **GBNF — по форме, не по содержимому.** План не специфицировал
   степень детализации; выбрано намеренно (обсуждено) — типизация
   per-tool дублировала бы `tools/schema.py` и всё равно не могла бы
   закрыть рантаймовые whitelist'ы (`location_id`).
3. **Системный промпт — файл + программный каталог, не готовый текст.**
   `config/system_prompt.txt` несёт только преамбул; каталог
   инструментов собирается из `tools/schema.py` каждый раз заново, ВЕСЬ
   (не отфильтрованный по состоянию) — иначе `CACHE_REUSE` на
   `llm_server` не работал бы (префикс менялся бы с каждым переходом
   состояния тура).
4. **Деградация — список бэкендов с retry, не stateful circuit
   breaker.** `llm_server/iros_llm_server_SPEC.md` §0 говорит про
   "список бэкендов и circuit breaker"; реально развёрнут один сервер
   (профили `qwen7b-q4`/`cpu-fallback` переключаются вручную через
   `.env`, не два живых эндпоинта одновременно) — резервировать
   cooldown-таймеры под несуществующий второй бэкенд не стали.
5. **Персистентная история между репликами — не реализована** (см.
   «Известные пробелы»), план явно не специфицирует её объём.
