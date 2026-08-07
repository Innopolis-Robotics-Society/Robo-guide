## 0. Что фактическая реализация меняет в плане ЛЛМ

Пять расхождений, каждое переписывает часть каталога инструментов.

| Факт | Следствие для ЛЛМ |
|---|---|
| `RunTour` — один goal, второй `REJECT`, не preempt | **Нет отдельного `goto`.** «Проводи к X» = `RunTour(location_ids=[X])`. Мид-тур переадресация требует cancel + новый goal |
| `AskUser`-сервера нет | Уточняющие вопросы ЛЛМ задаёт **сам** (Say + ожидание ASR + таймаут), внутри своего пакета. Не восстанавливать `AskUser` в FSM |
| `AWAITING_CONFIRM` ждёт `~/submit_confirm` (SetBool) | ЛЛМ обязан быть матчером ответа на «Идём дальше?» — иначе тур висит до `confirm_timeout_s`=20 |
| `ANSWERING` ждёт `submit_answer()` — **метод узла, не сервис** | Блокер. Без сервиса окно ответа всегда истекает по `answer_max_s`=45 |
| `narration_server` занят на время тура | Свободные реплики ЛЛМ идут через `Say` (scope dialog), **не** через `Narrate`. Иначе `REJECTED("busy")` |

Плюс: транзитного нарратива нет → UC «рассказываю по дороге» из v1 выпадает целиком, не закладывать.

---

## 1. Блокирующие правки в соседних пакетах

Делать **до** начала работы над ЛЛМ, иначе агент будет писаться против интерфейсов, которых нет.

**1.1 `guide_robot_msgs`: `SubmitAnswer.srv`**
```
uint8 OUTCOME_RESUME_BASE = 0   # вернуться в прерванное состояние
uint8 OUTCOME_SKIP_STOP   = 1   # «хватит, дальше»
uint8 OUTCOME_END_TOUR    = 2
uint8 outcome
---
bool accepted
string message
```
Не `SetBool` — исходов три. Это ровно тот выбор «resume vs next», который в раннем дизайне я отдавал ЛЛМ, а реализация зашила в авто-resume. Сервис возвращает его ЛЛМ, не ломая дефолт: не позвал — сработает таймаут и авто-resume.

**1.2 `mission_fsm`: `~/submit_answer`** — тонкая обёртка над существующим `FsmContext.submit_answer()`, симметрично тому, как на шаге 8 появились `~/request_pause`/`~/submit_confirm`. Плюс отдать в ответе сервиса текущий `resume_token` — ЛЛМ полезно знать, куда вернётся.

**1.3 `semantic_map`: убедиться, что `Location.public` реально фильтрует в `ListLocations`.** Whitelist ЛЛМ строится из этого поля; если фильтр не работает, служебка попадёт в промпт.

**1.4 Проверить, что `Say` с `SCOPE_DIALOG` не гасится `CancelAll(scope=narration)`.** Иначе ответ ЛЛМ будет умирать от собственного эха barge-in.

---

## 2. Решение по фреймворку — снимаю RAI

Раньше я советовал RAI как базу. С учётом фактического кода — снимаю. Аргументы против:

- ты уже дважды выбрал самописное вместо библиотеки (YASMIN → руками, BT → руками), и код это подтверждает: чистые модули без rclpy, свои таблицы переходов, ноль тяжёлых зависимостей;
- RAI тянет LangChain, свою модель узлов и HRI-топики `/from_human`/`/to_human`, которые придётся сшивать с твоими `/asr/transcript` + `Say.action`;
- официально R&D, активная разработка — пиннить ревизию, чинить самому.

Вместо неё: **свой tool-calling цикл, ~400 строк**, поверх OpenAI-совместимого HTTP (его отдают и `llama.cpp server`, и внешние провайдеры). Vendor-agnostic получается бесплатно, без LangChain. RAI остаётся как источник идей (структурированные трейсы, whoami-RAG), не как зависимость.

---

## 3. Состав `guide_robot_llm`

`ament_python`, три lifecycle-узла, свой `lifecycle_manager_llm` в launch (по прецеденту voice/mission — не в `supervisor.yaml`).

| Нода | Ответственность |
|---|---|
| `dialog_agent` | цикл tool-calling, кэш `/mission/state`, сборка снапшота, ожидание ASR для своих вопросов |
| `tool_broker` | единственный, кто держит клиентов к mission/semantic_map/voice; валидация + гейт по состоянию |
| `interaction_log` | jsonl-sink: транскрипт, снапшот, tool call, результат, версия контента, latency по стадиям |

`tool_broker` отдельно от `dialog_agent` намеренно: он тестируется на моках без бэкенда ЛЛМ вообще и остаётся рабочим, если агент выключен (CLI-режим).

**Чистая логика без rclpy** — по конвенции пакета:

| Модуль | Что |
|---|---|
| `tools/schema.py` | декларации инструментов + JSON-schema/GBNF, генерация из одного источника |
| `tools/validate.py` | whitelist локаций, допустимость по состоянию, нормализация аргументов |
| `snapshot.py` | `MissionState` + `Presence` + зона → компактный dict для промпта |
| `matching.py` | ASR-фраза → `confirm` yes/no, → выбор из `option_phrases` (нечёткий, RU) |
| `dialog/loop.py` | ReAct-шаг: сообщения → tool call → результат → сообщения; бэкенд инжектится |

---

## 4. Каталог инструментов под реальные интерфейсы

| Инструмент | Реальный вызов | Гейт |
|---|---|---|
| `start_tour(tour_id)` | `RunTour(tour_id)` | нет активного тура |
| `guide_to(location_id)` | `RunTour(location_ids=[id])` | то же |
| `tour_by_points(ids[])` | `EstimateRoute` → `RunTour(location_ids=ordered)` | то же |
| `stop_tour()` | cancel активного goal | тур активен |
| `pause()` / `resume()` | `~/request_pause` / `~/request_resume` | по состоянию |
| `confirm(yes\|no)` | `~/submit_confirm` | state == `AWAITING_CONFIRM` |
| `finish_answer(outcome)` | `~/submit_answer` **(новый)** | state == `ANSWERING` |
| `say(text)` | `Say`, PRIORITY_DIALOG / SCOPE_DIALOG | всегда |
| `tell_about(exhibit_id)` | `Narrate(exhibit_id)` | **только вне тура** |
| `list_locations(...)` / `list_tours()` | semantic_map srv | read-only |
| `estimate_route(ids)` | semantic_map srv | read-only |

`get_state` инструментом не делаем — состояние приходит в снапшоте каждый ход, кэш из `/mission/state` (TRANSIENT_LOCAL, поздний подписчик увидит текущее).

**Гейт по состоянию — в `tool_broker`, не в промпте.** ЛЛМ, попросивший `start_tour` во время тура, получает не `REJECT` от FSM, а внятный результат «тур уже идёт, доступно: stop_tour, pause» — и переспланирует. Это дешевле, чем ловить `REJECT`.

---

## 5. Снапшот

```json
{"mission": {"state": "NARRATING", "tour": "hall_a", "stop": 3, "of": 7,
             "location": "dinosaurs", "zone": "hall_2",
             "interrupt": {"kind": "answer", "base": "NARRATING"}},
 "presence": {"present": true, "last_evidence_s": 4},
 "safety": {"estop": false, "supervisor": "OPERATIONAL"},
 "nearby": ["dinosaurs", "cafe", "exit"],
 "tools_allowed": ["say", "finish_answer", "pause"]}
```

`tools_allowed` считается `tool_broker`'ом и кладётся в промпт — модель видит только то, что сейчас разрешено. Полный каталог локаций в промпт не идёт: `nearby` + `list_locations()` по запросу. Тексты экспонатов не идут никогда — они у `narration_server`.

---

## 6. Barge-in: что делает ЛЛМ

```
/speech/cancel_all (REASON_BARGE_IN)
  ├─ dialog_agent: abort in-flight HTTP-запрос к бэкенду, сброс частичного ответа
  ├─ FSM (уже сам): push answer-frame → ANSWERING, окно 45 с
  └─ dialog_agent: ждёт /asr/transcript (is_final) → цикл → say() → finish_answer()
```

Критично: **abort HTTP-запроса**, не просто игнор ответа. Иначе через 2 с придёт устаревшая реплика поверх новой. Это тот самый четвёртый шаг отмены, который я в первой версии дизайна пропустил.

Если ЛЛМ не уложился в `answer_max_s` — FSM резюмирует сам. Деградация корректная, но `interaction_log` должен это считать как отдельную метрику.

---

## 7. Этапы

| Шаг | Содержание | Готовность |
|---|---|---|
| 0 | `SubmitAnswer.srv`, `~/submit_answer`, проверка `public`/scope | тесты в mission_control зелёные |
| 1 | Скелет пакета, lifecycle, кэш `/mission/state`, `snapshot.py` | снапшот собирается на моках |
| 2 | `tool_broker` + `validate.py`, **без ЛЛМ** — сценарии дёргаются скриптом | полный тур с прерыванием проходит через broker |
| 3 | `matching.py` + подписка на ASR → `confirm`/`finish_answer` | `AWAITING_CONFIRM` закрывается голосом |
| 4 | Бэкенд: HTTP-абстракция, GBNF/schema, таймауты, лестница деградации, abort | mock-бэкенд + локальная модель |
| 5 | `dialog/loop.py`, промпт, лимит tool-call на ход (2) | end-to-end на моках |
| 6 | `interaction_log` + метрики латентности | |
| 7 | Живой прогон на железе | |

Шаг 2 — ключевой: после него система голосом не управляется, но **весь путь ЛЛМ→робот протестирован**. Замена скрипта на модель на шаге 5 не должна трогать broker.

---

## 8. Тестовая инфраструктура

Переиспользовать `test/mocks/harness.py` из mission_control (общий контекст, `sim_clock`). Новые моки: `mock_llm_backend.py` — HTTP-сервер, отдающий заранее заданные tool call'ы, включая невалидные (несуществующая локация, инструмент вне `tools_allowed`, битый JSON) — это тест guardrail'а, а не модели.

Блокирующие для мержа: `test_tool_gating.py` (каждый инструмент × каждое состояние), `test_barge_in_abort.py` (устаревший ответ не всплывает).

Не забыть `ROS_DOMAIN_ID` — те же грабли, что уже словил.

---

## 9. Риски

- **Латентность `confirm`.** Робот спросил «Идём дальше?» → ASR → ЛЛМ → `submit_confirm`. Если суммарно >3 с, посетитель повторит ответ. Для `confirm` и стоп-слов — обходить ЛЛМ: `matching.py` разбирает «да/нет/дальше/хватит» локально и дёргает сервис напрямую, ЛЛМ подключается только при непонятном ответе. Это отдельный быстрый путь, спроектировать сразу.
- **Язык прибит параметрами** (`language`, `content_language`). Двуязычность требует правок в mission/semantic_map, не в ЛЛМ. Не заявлять RU/EN в v1.
- **Пропуск остановки без объяснения.** `NARRATE_FAILED`/`NAV_FAILED` сейчас тихо пропускают остановку. Посетитель видит, что робот проехал мимо и молчит. Нужен минимум: `mission_fsm` публикует это в `/mission/state`, ЛЛМ комментирует. Иначе поведение читается как поломка.

Начинать с шага 0 — три мелкие правки в соседях, но без них шаг 2 упрётся.