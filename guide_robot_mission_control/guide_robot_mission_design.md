# guide_robot_mission — детальный проект (v1)

ament_python. Владелец состояния тура. Работает без ЛЛМ: полный тур гоняется из CLI и из тестов на моках.

---

## 0. Предусловия — проверить до начала работы

Эти вещи мission потребляет извне. Если чего-то нет — добавить **до** написания пакета, иначе придётся переписывать контракт.

### 0.1 `guide_robot_msgs` — что уже должно быть

| Интерфейс | Требование от mission |
|---|---|
| `Say.action` | В **result** обязаны быть поля фактического исполнения: `uint8 outcome`, `uint32 spoken_chars`, `float32 spoken_ms`, `string spoken_text`. Без этого невозможно ни построить `resume_token`, ни усечь контекст ЛЛМ по факту сказанного. Если полей нет — **это 6-е отклонение от спеки, задокументировать**. |
| `Say.action` | В **feedback** нужен момент реального начала озвучки (`bool started`, `builtin_interfaces/Time started_at`) — по нему narration_server запускает lookahead следующего чанка. Альтернатива: подписка на `SpeakingStatus` с корреляцией по `goal_id` — тогда `SpeakingStatus` должен нести `goal_id`. Выбрать одно, не оба. |
| `CancelAll` | Публикуется mission-ом со `scope="narration"` / `"dialog"`. Mission **не выдумывает epoch** — epoch бампает voice; mission читает актуальный epoch из `SpeakingStatus` только для логов и для поля `MissionState.epoch`. |
| barge-in msg на `/speech/barge_in` | Нужны как минимум `stamp` и `float32 confidence`. |
| `/system/events` | Нужен enum событий, из которых mission обязан реагировать: `SAFETY_HOLD`, `SAFETY_CLEAR`, `ESTOP`, `DEGRADED`, `SHUTDOWN_REQUEST`. |

### 0.2 Новые интерфейсы, которые надо добавить в `guide_robot_msgs`

`MissionState.msg`, `RunTour.action`, `Narrate.action`, `AskUser.action`, `NarrationControl.srv` — полные тексты в §2.

### 0.3 `guide_robot_semantic_map`

`ListLocations.srv`, `EstimateRoute.srv`, `GetExhibitContent.srv`. Для v1 mission-у достаточно:

- `GetExhibitContent(stop_id, lang, detail_level) -> (content_id, rev, text, duration_hint_s, ok, detail)`
  `rev` — версия текста (hex sha1[:8]); без него `resume_token` невалидируем.
- `EstimateRoute(from_pose|from_stop_id, to_stop_id) -> (distance_m, eta_s, reachable)`
- `ListLocations(tour_id) -> (stop_id[], name[], pose[], order[])`

### 0.5 Реконсиляция с фактическим состоянием репозитория (2026-08-05)

Этот раздел писался как v1-черновик до того, как устоялись реальные контракты
`guide_robot_msgs`, `guide_robot_voice`, `guide_robot_semantic_map`,
`guide_robot_supervisor`. К моменту реализации `guide_robot_mission_control`
(имя пакета — так, не `guide_robot_mission`: скелет уже создан под этим
именем) эти три пакета живые, протестированные, и `Say.action` /
`CancelAll.msg` / `SpeakingStatus.msg` / `SystemEvent.msg` /
`GetExhibitContent.srv` / `EstimateRoute.srv` / `ListLocations.srv` /
`ResolveLocation.srv` / `ListTours.srv` уже потребляются `guide_robot_voice`
и `guide_robot_llm`. Ломать их ради буквального соответствия §0.1/§0.2/§2
ниже — не вариант. Решение (подтверждено с владельцем репозитория):
**адаптировать mission под реальность, не наоборот.** `MissionState.msg`,
`Narrate.action`, `RunTour.action`, `AskUser.action` в `guide_robot_msgs`
существуют как пустые заглушки без единого потребителя — их можно
переопределить свободно, план ниже так и делает.

**`Say.action` (без изменений, `guide_robot_msgs/action/Say.action`).**
Result — `uint8 status` (не `outcome`), `string spoken_text`,
`uint32 spoken_chars`, `float32 spoken_duration` (не `spoken_ms`),
`string message`. Feedback — `clause_index/clause_count/progress/
current_clause` (клаузы — внутреннее деление `tts_node`, а не то, что
задумывалось в §4.1 как "чанк"). Момента `started` в feedback нет, но он и
не нужен: `SpeakingStatus.msg` уже несёт `goal_id` — это ровно та
альтернатива, что описана в §0.1 как admissible. `spoken_text`/
`spoken_chars` — источник правды для резюме; `resume.py` (§3) работает как
задумано, просто offset считает не сама, а получает готовым от `Say`.

**Чанкует не narration_server (§4.1 — отменяется).** `GetExhibitContent`
отдаёт `chunks: string[]` — уже готовые, заранее написанные и
провалидированные ревьюером фрагменты (`content/<exhibit_id>.<lang>.yaml`,
мягкий лимит 3 предложения на чанк, `guide_robot_semantic_map/lib/
content_io.py`). `narration_server` не режет текст вообще: один `Say` goal
на один элемент `chunks[]`. Более мелкое деление на клаузы для потоковой
отмены — уже внутри `tts_node`, невидимо снаружи `Say`. Зависимость
`guide_robot_mission_control → guide_robot_voice` (и `text_chunker`) из §4.1
не нужна; `exec_depend` на `guide_robot_voice` из пакета убрать.
`lookahead` (§4.2) остаётся как желательная оптимизация паузы между
элементами `chunks[]` (это уже не пауза между предложениями — они внутри
одного `Say`), но перестаёт быть архитектурно обязательным.

**Термин `content_id` → `exhibit_id` везде по документу** (`GetExhibitContent.
exhibit_id`, `mode` вместо `detail_level`, значения `mode` — только
`short|full`, **не** `transit`; `rev` → `version`). Грамматика `resume_token`
из §3.1 не меняется по форме, только по имени поля:
`v1|<exhibit_id>|<version>|<chunk_idx>|<char_off>`.

**Транзитный нарратив (§5.6) — источника контента с `mode=transit` не
существует** и добавлять его в `guide_robot_semantic_map` — вне рамок этой
задачи. v1 mission использует только общий пул реплик из `phrases_ru.yaml`
на ходу (второй источник из §5.6 остаётся, первый — `GetExhibitContent(...,
detail_level="transit")` — вычёркивается). Если экспозиционный transit-контент
понадобится, это отдельная задача на `guide_robot_semantic_map`.

**Реальный контракт `guide_robot_semantic_map` (все три сервиса уже
реализованы, без изменений):**

```
GetExhibitContent(exhibit_id, mode, language) -> (chunks: string[], version: string)
EstimateRoute(ids: string[], optimize: bool)
    -> (ordered_ids: string[], distance_m, duration_min, feasible: bool)
    # старт маршрута -- ТЕКУЩАЯ поза робота (TF), не первый элемент ids.
    # eta одного перегона = EstimateRoute(ids=[stop_id], optimize=false).
ListLocations(zone, category, near_only) -> (locations: Location[])
    # без фильтра по id; список кэшируется mission-ом и индексируется локально.
ListTours(language) -> (tours: Tour[])
    # Tour{id, name, duration_min_estimate, TourStop[] stops}
    # TourStop{location_id, exhibit_id, dwell_s, mode}
    # RunTour.tour_id разворачивается в остановки через ListTours, а не
    # через ListLocations(tour_id) -- такого параметра у ListLocations нет.
ResolveLocation(query, language, max_results) -> (candidates, scores, confident)
    # нечёткий резолв алиасов; mission её не вызывает напрямую в v1
    # (это путь LLM/диалога), только для полноты картины.
```

**`/system/events` (§0.1, §5.7) не существует.** Реальный источник
safety/degraded-сигналов — `guide_robot_supervisor`:
`/supervisor/estop` (`std_msgs/Bool`, latched-семантики нет — считать
протухшим не нужно, паблишер живёт постоянно) и `/supervisor/state`
(`std_msgs/String`, значения `INIT|BRINGUP|ACTIVE|DEGRADED|RECOVERING|
FAULT|SHUTDOWN`). Соответствие событиям §5.7:
`estop==true` → `SAFETY_HOLD`/`ESTOP` (mission не различает — оба ведут в
`HELD`), `estop==false` (после было `true`) → `SAFETY_CLEAR`,
`state=="DEGRADED"` → `DEGRADED` (запретить новые туры), `state in
("SHUTDOWN", "FAULT")` → `SHUTDOWN_REQUEST`. Отдельного топика на
"L2-объяснение" тоже нет — mission просто публикует `Say` scope=SAFETY, как
и было задумано. Своя диагностика mission (не команды, а INFO/WARN/ERROR)
идёт, как у всех остальных пакетов, в `/system_event`
(`guide_robot_msgs/msg/SystemEvent`, поля `id: string, severity, detail`).

**`/speech/barge_in` (§0.1, §4.2) не существует.** Barge-in целиком внутри
`vad_node`: при срабатывании VAD он сам публикует `CancelAll` на
`/speech/cancel_all` с `reason=CancelAll.REASON_BARGE_IN` — отдельного
топика с `confidence` нет, порог уже применён внутри `vad_node`
(`barge_in_min_windows` и т.п.). `narration_server` и `mission_fsm`
подписываются на тот же `/speech/cancel_all` и реагируют на
`reason == REASON_BARGE_IN`, вместо выдуманного `/speech/barge_in`. Это не
меняет архитектурное решение §4.2 ("narration_server слушает сам, не через
FSM") — меняется только имя топика и то, что порог уверенности не
конфигурируется на стороне mission (`barge_in_min_confidence` из
`config/mission.yaml`, §8, — вычёркивается как несуществующий на этом
уровне параметр).

**`CancelAll.msg` (без изменений).** Уже имеет `scope`
(`SCOPE_ALL/NARRATION/DIALOG/SAFETY` — числа совпадают с `Say.action`) и
`epoch`, ровно как требует §0.1: mission эти значения не выдумывает,
эпоху бампает voice, mission публикует со своим scope и читает epoch
только для логов/`MissionState.epoch`.

**Итог по порядку реализации (§12).** Шаг 1 не "добавить интерфейсы" в
чистом виде — это переопределить 4 существующих заглушки
(`MissionState.msg`, `Narrate.action`, `RunTour.action`, `AskUser.action`)
под грамматику §2 (адаптированную выше) и добавить один новый файл
(`NarrationControl.srv`), не трогая ничего, что уже используется
`guide_robot_voice`/`guide_robot_llm`/`guide_robot_semantic_map`.

---

## 1. Раскладка пакета

```
guide_robot_mission/
  package.xml
  setup.py
  setup.cfg
  guide_robot_mission/
    __init__.py
    resume.py                  # грамматика resume_token, чистая функция, без ROS
    interrupt_stack.py         # стек прерываний глубины 1, без ROS
    chunk_plan.py              # план чанков + учёт произнесённого, без ROS
    fsm/
      __init__.py
      outcomes.py              # общие строковые исходы
      base.py                  # InterruptibleState: поллинг флагов, cancel_state()
      root_sm.py               # верхняя SM
      tour_sm.py               # вложенная SM тура (NAVIGATING/NARRATING/AWAITING_CONFIRM)
      states/
        idle.py greeting.py navigating.py narrating.py
        answering.py awaiting_confirm.py paused.py held.py returning.py
      blackboard_keys.py       # типизированные ключи + геттеры
    clients/
      nav_client.py            # NavigateToPose, cancel, feedback→eta
      say_client.py            # Say + CancelAll, единая точка приоритетов/scope
      semantic_client.py       # три сервиса, с таймаутами и кэшем контента
    mission_fsm_node.py
    narration_server_node.py
    presence_monitor_node.py
    cli.py                     # ros2 run guide_robot_mission mission_cli ...
  config/
    mission.yaml
    phrases_ru.yaml            # системные фразы (одна за раз, продолжаю, не расслышал…)
  launch/
    mission.launch.py
  test/
    mocks/
      mock_nav_server.py
      mock_say_server.py
      mock_semantic_map.py
      sim_clock.py             # публикатор /clock для мгновенных таймаутов
      harness.py               # поднятие узлов+моков в одном MultiThreadedExecutor
    test_resume_token.py
    test_chunk_plan.py
    test_interrupt_stack.py
    test_narration_resume.py   # параметризован по точке прерывания
    test_tour_flow.py
    test_ask_user.py
    test_presence.py
    test_safety_hold.py
    test_copyright.py
```

`ros2 pkg create` → удалить `test_pep257.py` и `test_flake8.py`, оставить `test_copyright.py`.

Три исполняемых узла: `mission_fsm`, `narration_server`, `presence_monitor`. Плюс `mission_cli`.
Плюс entry point `mission_container` — запускает `mission_fsm` и `narration_server` в одном процессе на общем `MultiThreadedExecutor`. Это дефолт в `mission.launch.py`: снимает сериализацию на пути barge-in → пауза нарратива. Отдельные узлы остаются доступны для отладки.

---

## 2. Интерфейсы

### 2.1 `MissionState.msg`

```
std_msgs/Header header

uint8 STATE_IDLE=0
uint8 STATE_GREETING=1
uint8 STATE_NAVIGATING=2
uint8 STATE_NARRATING=3
uint8 STATE_ANSWERING=4
uint8 STATE_AWAITING_CONFIRM=5
uint8 STATE_PAUSED=6
uint8 STATE_HELD=7
uint8 STATE_RETURNING=8
uint8 state

uint8 IRQ_NONE=0
uint8 IRQ_ANSWERING=1
uint8 IRQ_AWAITING_CONFIRM=2
uint8 interrupt              # верхушка стека прерываний
uint8 base_state             # состояние под прерыванием (== state, если IRQ_NONE)

uint8 PAUSE_NONE=0
uint8 PAUSE_USER=1
uint8 PAUSE_SAFETY=2
uint8 PAUSE_PRESENCE=3
uint8 pause_reason

string tour_id
uint16 stop_index
uint16 stop_total
string stop_id

string content_id
uint16 chunk_index
uint16 chunk_total
bool   resume_available
string resume_token

bool   presence
uint32 epoch                 # последний известный epoch voice, для логов
string detail                # человекочитаемая причина последнего перехода
```

QoS: `RELIABLE`, `TRANSIENT_LOCAL`, `depth 1`. Публикуется **при каждом переходе** и не чаще, чем раз в `state_min_period_s` (дефолт 0.1) при частых изменениях `chunk_index`; плюс безусловный heartbeat раз в 1 с — на нём висит `TopicRateWatchdog` супервизора.

### 2.2 `Narrate.action`

```
# --- goal ---
string content_id          # обязателен; ключ для GetExhibitContent и валидации resume
string text                # если пусто — narration_server сам дёрнет GetExhibitContent
string resume_token        # "" = с начала
uint8  priority            # прокидывается в Say
string scope               # дефолт "narration"
uint8 CONTINUITY_CONTINUOUS=0   # связный текст: мягкая пауза сохраняет resume_token
uint8 CONTINUITY_DROPPABLE=1    # набор независимых реплик: остаток выбрасывается, токен пустой
uint8  continuity
---
# --- result ---
uint8 OUTCOME_COMPLETED=0
uint8 OUTCOME_PAUSED=1        # мягкая пауза, есть resume_token
uint8 OUTCOME_INTERRUPTED=2   # жёсткий обрыв (barge-in), есть resume_token
uint8 OUTCOME_ABORTED=3
uint8 OUTCOME_REJECTED=4      # занят другим goal / контент не найден
uint8  outcome
string resume_token
uint16 chunks_spoken
uint16 chunks_total
string spoken_text            # реально произнесённый префикс — источник правды для усечения контекста ЛЛМ
string detail
---
# --- feedback ---
uint16 chunk_index
uint16 chunk_total
string chunk_text
float32 progress              # 0..1 по символам, не по чанкам
```

### 2.3 `NarrationControl.srv`

Пауза с полезной нагрузкой в ответе — через `cancel` её не передать.

```
uint8 MODE_SOFT=0     # доиграть текущий чанк (и уже отправленный lookahead), дальше не слать
uint8 MODE_HARD=1     # немедленно: CancelAll(scope=narration) + отмена активных Say
uint8 mode
string reason
---
bool   ok
string resume_token
uint16 chunks_spoken
```

Голый `cancel` активного `Narrate` goal эквивалентен `MODE_HARD`.

### 2.4 `RunTour.action`

```
# --- goal ---
string tour_id
string[] stop_ids          # override; пусто → взять из ListLocations(tour_id)
uint16 start_index         # рестарт с середины
bool   greet               # дефолт true
bool   narrate             # false = только объезд, для проверки навигации
bool   confirm_between_stops   # спрашивать «идём дальше?»
bool   return_home
---
# --- result ---
uint8 OUTCOME_COMPLETED=0
uint8 OUTCOME_CANCELED=1
uint8 OUTCOME_ABORTED=2
uint8 OUTCOME_NO_VISITOR=3     # дисengagement по presence
uint8  outcome
uint16 stops_completed
uint16 stops_skipped
string detail
---
# --- feedback ---
uint8  phase              # = MissionState.state
uint16 stop_index
uint16 stop_total
string stop_id
float32 eta_s             # из nav feedback либо EstimateRoute
```

Один активный `RunTour` goal. Второй — `REJECT` (не preempt: тур — это владение роботом целиком).

### 2.5 `AskUser.action`

Неблокирующий по смыслу FSM: goal живёт долго, но FSM не ждёт его в `execute()` — состояние + таймер.

```
# --- goal ---
string question
string[] option_ids        # пусто = свободный ответ
string[] option_phrases    # синонимы через '|' для матчинга: "да|давай|поехали"
float32 timeout_s          # 0 → из конфига (confirm_timeout_s)
bool   speak_question      # дефолт true
uint8  ON_TIMEOUT_DEFAULT=0
uint8  ON_TIMEOUT_ABORT=1
uint8  ON_TIMEOUT_REPEAT_ONCE=2
uint8  on_timeout
string default_answer
---
# --- result ---
uint8 OUTCOME_ANSWERED=0
uint8 OUTCOME_TIMEOUT=1
uint8 OUTCOME_CANCELED=2
uint8 OUTCOME_PREEMPTED=3     # вытеснено safety hold
uint8 OUTCOME_REJECTED=4      # стек занят → «давайте по одному»
uint8  outcome
string answer                 # option_id либо сырой текст при свободном ответе
string raw_text
float32 confidence
---
# --- feedback ---
uint8 STAGE_ASKING=0
uint8 STAGE_LISTENING=1
uint8 STAGE_REPEATING=2
uint8 stage
float32 remaining_s
```

---

## 3. `resume_token` и учёт произнесённого

### 3.1 Грамматика (`resume.py`, чистые функции, тесты без ROS)

```
v1|<content_id>|<rev>|<chunk_idx>|<char_off>
```

- `rev` — `sha1(text).hexdigest()[:8]`, приходит из `GetExhibitContent`.
- `chunk_idx` — индекс первого **не завершённого полностью** чанка.
- `char_off` — сколько символов этого чанка реально произнесено (из `Say.result.spoken_chars`).
- Пустая строка — валидный токен «с начала».

Валидация при возобновлении:
1. `content_id` не совпал → `REJECTED`, лог error (звонок не туда).
2. `rev` не совпал → контент переиздан: начать с `chunk_idx=0`, `detail="content_rev_changed"`, предупреждение в лог. Не падать.
3. `chunk_idx >= len(chunks)` → считать нарратив завершённым, `COMPLETED` немедленно.

### 3.2 Политика возобновления

Параметр `resume_policy`:

| Значение | Поведение |
|---|---|
| `repeat_chunk` (**дефолт**) | перечитать прерванный чанк целиком с начала. `char_off` используется только для `spoken_text`, не для точки старта. |
| `continue_next` | считать прерванный чанк потерянным, начать со следующего. |
| `overlap_1` | начать с `max(0, chunk_idx-1)`. |

Перед возобновлением, если `chunks_spoken > 0`, произносится мостовая фраза из `phrases_ru.yaml` (`resume_bridge: "Продолжаю."`) отдельным `Say` со `scope="system"`. Отключается флагом `resume_bridge_enabled`.

Причина дефолта: `TextChunker` режет по предложениям, повтор предложения естественен для слушателя; склейка с середины предложения — нет.

### 3.3 `chunk_plan.py` — учёт

Чистый класс без ROS, полностью покрывается юнит-тестами.

```python
class ChunkState(IntEnum):
    PENDING = 0    # ещё не отправлен в Say
    SENT = 1       # goal принят, но озвучка не началась (lookahead)
    SPEAKING = 2   # пришёл started
    DONE = 3       # Say result COMPLETED
    CUT = 4        # Say result отменён/оборван, spoken_chars < len
```

```python
class ChunkPlan:
    def __init__(self, content_id, rev, chunks: list[str], start_idx=0)
    def next_to_send(self) -> int | None          # первый PENDING, если в полёте < lookahead+1
    def mark(self, idx, state, spoken_chars=0)
    def resume_token(self) -> str                 # первый не-DONE чанк
    def spoken_text(self) -> str                  # конкатенация DONE + префикс CUT
    def progress(self) -> float                   # по символам
    def is_complete(self) -> bool
```

**Инвариант, проверяемый тестом:** для любой последовательности прерываний и возобновлений конкатенация всех `spoken_text` по сегментам содержит полный исходный текст как подпоследовательность в исходном порядке; ни один чанк не пропущен (при `repeat_chunk` допускаются повторы, при `continue_next` — не более одного пропуска на прерывание, и это фиксируется явным ассертом).

---

## 4. `narration_server`

Lifecycle-нода. Единственный клиент — `mission_fsm` (но интерфейс публичный, CLI может дёргать напрямую).

### 4.1 Кто чанкует

**Чанкует narration_server**, один `Say` goal на чанк. Не voice целиком одним куском.

Обоснование: границы чанков — единица паузы и возобновления, они обязаны быть видимы владельцу состояния. Если чанкует voice, то мягкая пауза и `resume_token` требуют протаскивания индексов чанков через feedback `Say` — тот же объём работы, но контроль уезжает в пакет, который не владеет состоянием.

Реализация: `from guide_robot_voice.text_chunker import TextChunker`, `<exec_depend>guide_robot_voice</exec_depend>`. Дублировать чанкер запрещено — расхождение реализаций даст рассинхрон границ. Если зависимость mission→voice покажется нежелательной, единственная допустимая альтернатива — вынести `TextChunker` в отдельный чистый python-пакет `guide_robot_text`; но это отдельная задача, не в рамках v1.

### 4.2 Конвейер и lookahead

Пауза между предложениями = round-trip `Say`. Чтобы её не было, держим `lookahead` (дефолт **1**) чанков в полёте: чанк `k+1` отправляется, как только чанк `k` доложил `started`.

| lookahead | Эффект |
|---|---|
| 0 | детерминизм, слышимая пауза между предложениями. Используется в части тестов. |
| 1 | дефолт. Мягкая пауза срабатывает не позже, чем через 2 чанка. |
| ≥2 | запрещено в v1: растёт хвост, который нельзя отменить мягко. |

**Мягкая пауза (`MODE_SOFT`):** перестать слать новые чанки; дождаться завершения `SENT`/`SPEAKING`; вернуть `PAUSED` + токен. Ограничить ожидание `soft_pause_max_s` (дефолт 8) — по истечении эскалировать в `MODE_HARD`.

**Жёсткая остановка (`MODE_HARD`, barge-in, cancel):** опубликовать `CancelAll(scope="narration")` → voice бампает epoch и фенсит; параллельно отменить все активные `Say` goal-ы. Не ждать результатов дольше `hard_stop_result_timeout_s` (дефолт 0.3): если результат не пришёл, считать `spoken_chars` по последнему feedback, а чанк — `CUT`. Возврат `INTERRUPTED` + токен обязан произойти в пределах этого таймаута, иначе FSM подвиснет на пути прерывания.

**Аудио-остановка не идёт через mission.** Хот-пас — это `vad_node → /speech/barge_in → voice` (внутри voice: бамп epoch, флаш буфера). `narration_server` слушает `/speech/barge_in` **сам** (не через FSM) и синхронно переводит план в `CUT`; `mission_fsm` тем же сообщением поднимает состояние `ANSWERING`. Оба — подписчики одного топика, а не звенья цепочки. `CancelAll` от narration_server — страховка на случай, если voice-овый путь не сработал; повторный бамп epoch идемпотентен по смыслу (лишний бамп безопасен, пропущенный — нет).

### 4.3 Параметры `Say` по scope

| Контекст | scope | priority | interruptible |
|---|---|---|---|
| Чанк нарратива | `narration` | NORMAL | true |
| Ответ на вопрос | `dialog` | NORMAL+1 | true |
| Вопрос AskUser | `dialog` | NORMAL+1 | true |
| «Давайте по одному», «Продолжаю», «Не расслышал» | `system` | HIGH | **false** (фразы короткие, обрывать нечего) |
| Safety-объяснение (L2) | `system` | HIGHEST | false |

### 4.4 Одновременность

Один активный `Narrate` goal. Второй → `REJECTED` с `detail="busy"`. Preempt не поддерживается: владелец один, конкуренции быть не должно; если она возникла — это баг FSM, и он должен быть громким.

---

## 5. `mission_fsm`

Lifecycle-нода. Владелец: состояние тура, стек прерываний, публикация `MissionState`.

### 5.1 Почему не «чистая» YASMIN

Переходы YASMIN статичны, а прерывание может прилететь в любом длинном состоянии. Схема:

- Все длинные состояния наследуют `InterruptibleState` (`fsm/base.py`): внутри — цикл `while` с шагом 20 мс, который опрашивает `Event`-флаги на блэкборде (`irq_barge_in`, `irq_ask`, `irq_safety`, `irq_pause`, `irq_cancel`) и состояние своего action goal. Реализованы `cancel_state()`.
- Состояние возвращает не только `succeeded`/`aborted`, но и `interrupted`/`held`/`paused`/`canceled`. В блэкборд кладётся, чем именно прервано и с каким `resume_token`.
- Верхняя SM маршрутизирует эти исходы в `ANSWERING` / `AWAITING_CONFIRM` / `HELD` / `PAUSED`; на выходе из них — `resume` → назад в базовое состояние, которое читает `resume_token` из блэкборда.

Ни одно состояние **не** делает блокирующий `spin_until_future_complete`. Все клиенты — асинхронные, узел крутится на `MultiThreadedExecutor`, FSM живёт в своём потоке, коллбэки подписок — в `ReentrantCallbackGroup`.

### 5.2 Таблица переходов верхней SM

| Состояние | Исход | → |
|---|---|---|
| `IDLE` | `tour_requested` | `GREETING` |
| | `shutdown` | (конец SM) |
| `GREETING` | `succeeded` | `NAVIGATING` |
| | `interrupted` | `ANSWERING` |
| | `no_visitor` | `IDLE` |
| `NAVIGATING` | `arrived` | `NARRATING` |
| | `nav_failed` | `AWAITING_CONFIRM` (спросить: пропустить экспонат?) |
| | `interrupted` | `ANSWERING` |
| | `held` / `paused` / `canceled` | `HELD` / `PAUSED` / `RETURNING` |
| `NARRATING` | `succeeded` | `AWAITING_CONFIRM` (если `confirm_between_stops`) иначе `NAVIGATING` |
| | `tour_finished` | `RETURNING` |
| | `interrupted` | `ANSWERING` |
| | `held` / `paused` / `canceled` | `HELD` / `PAUSED` / `RETURNING` |
| `ANSWERING` | `answered` / `timeout` | `resume_base` (диспетчер) |
| | `held` | `HELD` |
| `AWAITING_CONFIRM` | `yes` | `NAVIGATING` |
| | `no` | `RETURNING` |
| | `timeout` | по `on_timeout` |
| | `interrupted` | остаётся в себе, перезапускает вопрос (см. 5.4) |
| `PAUSED` | `resumed` | `resume_base` |
| | `timeout_no_visitor` | `RETURNING` |
| `HELD` | `cleared` | `resume_base` |
| | `hold_timeout` | `RETURNING` |
| `RETURNING` | `home` / `failed` | `IDLE` |

`resume_base` — не состояние, а псевдо-переход: диспетчер снимает верхний фрейм стека и возвращает управление в `frame.base_state`, положив в блэкборд его `resume_token`.

Вложенная `tour_sm` (`NAVIGATING`/`NARRATING`/`AWAITING_CONFIRM` + счётчик остановок) отделена от верхней, чтобы `IDLE`/`HELD`/`PAUSED`/`RETURNING` не тащили за собой контекст тура.

### 5.3 Ключи блэкборда

```
tour: TourPlan            # stop_ids, index, options, deadline
nav_goal_handle
narrate_goal_handle
resume_token: str
irq: InterruptRequest|None    # kind, payload, stamp
stack: InterruptStack
presence: PresenceView        # present, since, last_evidence
safety: SafetyView            # held, reason
last_answer: str
```

Блэкборд пишется **только** из потока FSM. Коллбэки подписок кладут запрос в `queue.Queue` и взводят `Event`; FSM забирает. Это единственный способ не ловить гонки при `MultiThreadedExecutor`.

### 5.4 Стек прерываний (глубина 1)

```python
@dataclass(frozen=True)
class Frame:
    kind: Literal["answer", "confirm"]
    base_state: str
    resume_token: str
    opened_at: Time
    deadline: Time
```

Правила:

1. `push()` при пустом стеке — всегда успешен.
2. **`AskUser` при занятом стеке** → `REJECTED`, робот произносит `phrases.one_at_a_time` («Давайте по одному, я сейчас закончу»). Фрейм не меняется.
3. **barge-in при фрейме `answer`** → фрейм **не** пушится и **не** снимается: текущий ответ обрывается (Say cancel), фрейм переиспользуется под новую реплику, `opened_at` обновляется, `deadline` — нет. Пользователь переспросил, это не вложенность.
4. **barge-in при фрейме `confirm`** → фрейм `confirm` заменяется на `answer` c тем же `base_state` и `resume_token`; после ответа вопрос задаётся заново (`stage=REPEATING`), не более `confirm_repeat_max` раз (дефолт 1).
5. **Safety hold** — не фрейм. `HELD` вытесняет всё: активный `AskUser` goal завершается `PREEMPTED`, `Narrate` — `MODE_HARD`, фрейм сохраняется как есть и восстанавливается после `SAFETY_CLEAR`.
6. `answer_max_s` (дефолт 45) — принудительный `pop` фрейма `answer` с `detail="answer_timeout"`. Защита от зависшего ЛЛМ, который в v1 отсутствует, но контракт должен существовать заранее.

### 5.5 Цикл тура (свой, не `nav2_waypoint_follower`)

```
GREETING:      Say(приветствие, scope=dialog)  [если greet]
for i in range(start_index, len(stops)):
    NAVIGATING:  EstimateRoute → feedback eta
                 NavigateToPose(stop.pose)
                 параллельно: Narrate(transit_content, continuity=DROPPABLE)  [если transit_narration]
                 на arrived → NarrationControl(MODE_SOFT), доиграть текущий чанк
                 таймаут nav_stop_timeout_s → nav_failed
    NARRATING:   GetExhibitContent(stop_id) → Narrate(content_id)
    AWAITING_CONFIRM (если confirm_between_stops): «Идём дальше?»
RETURNING:     NavigateToPose(home) [если return_home]
IDLE
```

`GetExhibitContent` для остановки `i+1` префетчится во время `NARRATING` остановки `i` (кэш в `semantic_client`), чтобы после прибытия не было паузы на сервис.

`nav_failed` не абортит тур: `AWAITING_CONFIRM` с вопросом «Не могу подъехать, рассказать отсюда / пропустить?» и `on_timeout=ASSUME_DEFAULT`, дефолт — пропустить (`stops_skipped++`).

### 5.6 Речь во время движения

Робот не едет молча. `NavigateToPose` и `Narrate` — независимые клиенты, состояние `NAVIGATING` владеет обоими.

**Контент транзита — не экспозиционный.** Момент прибытия непредсказуем (8 с или 40 с на один и тот же перегон), поэтому текст на ходу обязан обрываться на любой границе чанка без семантической потери: набор независимых коротких реплик («идём в следующий зал», «слева от вас…»), а не связное повествование. Отсюда `continuity=DROPPABLE`: при мягкой паузе `resume_token` не сохраняется, остаток выбрасывается.

Источник: `GetExhibitContent(stop_id, detail_level="transit")` для остановки-цели, либо общий пул реплик из `phrases_ru.yaml` при отсутствии контента. Запрос уходит вместе с префетчем экспозиционного контента.

**Информационная плотность на ходу — низкая, и это требование, а не стилистика.** Колонка направлена вперёд, посетитель идёт сзади-сбоку, дистанция гуляет, шум приводов поднимает пол шума. Разборчивость в движении заметно хуже, чем на остановке лицом к человеку. Факты об экспонате произносятся только стоя.

**На прибытии — `MODE_SOFT`.** Робот доигрывает текущее предложение уже у экспоната, затем начинает экспозиционный нарратив. `HARD` только по safety.

**Ограничение по длительности:** транзитный `Narrate` не запускается, если `EstimateRoute.eta_s < transit_min_eta_s` (дефолт 6.0) — на коротком перегоне реплика не успеет закончиться и будет выглядеть обрубленной.

**barge-in на ходу.** Параметр `pause_nav_on_barge_in`, дефолт `true`: `NavigateToPose` отменяется, робот отвечает стоя, после снятия фрейма goal переотправляется тем же pose (плата — перепланирование). Обоснование дефолта: разговор с едущим роботом вынуждает посетителя идти рядом и говорить в спину, а распознавание в движении и так деградировано. При `false` навигация продолжается, что предпочтительно в узких проходах.

**Известное ограничение до XVF3800:** barge-in именно *в движении* ненадёжен — шум приводов и собственная речь без AEC бьют по VAD и wake-word одновременно. На стадиях 1–2 прототипа (наушники) это не проявится.

В `MissionState` поля `content_id` / `chunk_index` / `chunk_total` валидны и в состоянии `NAVIGATING` — состояние FSM и факт речи ортогональны.

### 5.7 Обработка `/system/events`

| Событие | Действие |
|---|---|
| `SAFETY_HOLD`, `ESTOP` | `Narrate` MODE_HARD, `NavigateToPose` cancel, → `HELD`, `pause_reason=SAFETY`. Объяснение (L2) произносится **отдельным** `Say` scope=system, не через narration_server. |
| `SAFETY_CLEAR` | `HELD` → `resume_base`. Если стояли > `held_resume_reannounce_s` (дефолт 15) — мостовая фраза перед возобновлением. |
| `DEGRADED` | остаться в состоянии, поднять флаг в `detail`, запретить старт новых туров. |
| `SHUTDOWN_REQUEST` | завершить `RunTour` как `CANCELED`, `RETURNING` пропустить, → `IDLE`, дать supervisor-у деактивировать. |

Mission **не** дублирует логику остановки движения. L1-стоп принадлежит супервизору и живёт независимо; mission только приводит своё состояние в соответствие.

---

## 6. `presence_monitor`

Lifecycle-нода. Источники присутствия конфигурируемы списком; каждый — «свидетельство» с меткой времени.

| Источник | Топик | Учитывать |
|---|---|---|
| wakeword | `/voice/wakeword` | если `confidence >= wakeword_min_confidence` |
| финальный ASR | `/asr/final` | всегда |
| VAD | `/voice/vad` | **только если `not tts_active`** (см. ниже) |
| люди (задел) | `/perception/people` | если `count > 0`; в v1 источник отсутствует, узел не должен падать при его отсутствии |
| прибытие на остановку | `/mission/state` | опционально как слабое свидетельство, `weak_evidence: false` по умолчанию |

**Критично до XVF3800:** без аппаратного AEC собственная речь робота триггерит VAD, и присутствие будет вечно «истинным». Флаг `ignore_vad_while_speaking: true` (дефолт) — отбрасывать VAD-свидетельства, пока `SpeakingStatus.speaking == true` плюс `tts_tail_ms` (дефолт 300) после. Когда приедет ReSpeaker — флаг переводится в `false` одной строкой конфига.

Логика:
- `present` взводится немедленно по любому свидетельству;
- `present` снимается через `disengage_timeout_s = 120.0` без свидетельств (значение из исследования Alter-Ego);
- дополнительно публикуется `seconds_since_evidence`, чтобы FSM мог сам вводить более короткие пороги (например, 20 с ожидания ответа на confirm).

Публикует `/mission/presence` (`RELIABLE TRANSIENT_LOCAL depth 1`, heartbeat 1 Гц):

```
std_msgs/Header header
bool present
builtin_interfaces/Time last_evidence
float32 seconds_since_evidence
string last_source
```

---

## 7. QoS-таблица

| Топик/интерфейс | Тип | QoS |
|---|---|---|
| `/mission/state` | pub | RELIABLE, TRANSIENT_LOCAL, depth 1 |
| `/mission/presence` | pub | RELIABLE, TRANSIENT_LOCAL, depth 1 |
| `/speech/barge_in` | sub | RELIABLE, VOLATILE, depth 10 |
| `/system/events` | sub | RELIABLE, TRANSIENT_LOCAL, depth 10 |
| `/voice/speaking_status` | sub | RELIABLE, VOLATILE, depth 10 |
| `/voice/cancel_all` | pub | RELIABLE, VOLATILE, depth 10 |
| `/asr/final` | sub | RELIABLE, VOLATILE, depth 10 |
| Actions | — | дефолтные rcl_action QoS |

---

## 8. `config/mission.yaml`

```yaml
/**:
  ros__parameters:
    use_sim_time: false

mission_fsm:
  ros__parameters:
    tour:
      default_tour_id: "main"
      confirm_between_stops: true
      transit_narration: true
      transit_min_eta_s: 6.0
      pause_nav_on_barge_in: true
      return_home: true
      home_frame: "map"
      home_pose: [0.0, 0.0, 0.0]        # x, y, yaw
    timeouts:
      nav_stop_timeout_s: 180.0
      answer_max_s: 45.0
      confirm_timeout_s: 20.0
      confirm_repeat_max: 1
      held_max_s: 300.0
      held_resume_reannounce_s: 15.0
      disengage_timeout_s: 120.0
      service_call_timeout_s: 2.0
    interrupts:
      stack_depth: 1                    # менять запрещено, параметр только для тестов
      barge_in_min_confidence: 0.6
      barge_in_debounce_ms: 250
    state_pub:
      min_period_s: 0.1
      heartbeat_s: 1.0

narration_server:
  ros__parameters:
    lookahead: 1
    resume_policy: "repeat_chunk"       # repeat_chunk | continue_next | overlap_1
    resume_bridge_enabled: true
    soft_pause_max_s: 8.0
    hard_stop_result_timeout_s: 0.3
    say_priority_narration: 100
    say_scope_narration: "narration"
    chunker:
      max_chars: 220
      min_chars: 40

presence_monitor:
  ros__parameters:
    disengage_timeout_s: 120.0
    wakeword_min_confidence: 0.6
    ignore_vad_while_speaking: true
    tts_tail_ms: 300
    sources: ["wakeword", "asr_final", "vad"]
    publish_rate_hz: 1.0
```

---

## 9. Тесты

Всё гоняется в CI без железа и без Gazebo: только rclpy + моки.

### 9.1 Моки

**`mock_say_server.py`** — сердце тестов. Должен уметь:
- «произносить» текст по виртуальным часам со скоростью `chars_per_sec` (дефолт 15);
- публиковать feedback `started` и `SpeakingStatus`;
- корректно отдавать `spoken_chars` при отмене в произвольной точке;
- подписываться на `CancelAll` и фенсить по scope/epoch — иначе не проверить, что narration_server не шлёт в мёртвый epoch;
- инъекция отказов: `fail_on_chunk`, `delay_result_s`, `never_return_result` (для теста `hard_stop_result_timeout_s`).

**`mock_nav_server.py`**: `NavigateToPose` с настраиваемой длительностью, feedback с `distance_remaining`, поддержкой cancel, режимами `succeed`/`abort`/`hang`.

**`mock_semantic_map.py`**: три сервиса, контент из YAML-фикстуры, режим `change_rev_after_n_calls` для теста несовпадения `rev`.

**`sim_clock.py`**: узлы поднимаются с `use_sim_time:=true`, тест публикует `/clock` и прокручивает время. 120-секундный дисengagement проверяется за миллисекунды. Без этого тесты присутствия и таймаутов в CI нежизнеспособны.

### 9.2 Обязательные кейсы

| Файл | Кейсы |
|---|---|
| `test_resume_token.py` | round-trip parse/format; битый токен; чужой `content_id`; `chunk_idx` за границей; пустой токен |
| `test_chunk_plan.py` | инвариант полноты (см. 3.3) для случайных последовательностей прерываний — property-based, 200 прогонов с фиксированным seed |
| `test_narration_resume.py` | **параметризован по `k` от 0 до N-1**: прерывание на каждом чанке → resume → сверка итогового `spoken_text`. Отдельно: `lookahead=0` и `lookahead=1`; `resume_policy` во всех трёх значениях; прерывание в момент `SENT`, но до `started` (гонка lookahead); двойное прерывание подряд; прерывание на последнем чанке |
| `test_interrupt_stack.py` | AskUser поверх answer → `REJECTED` + фраза «по одному»; barge-in поверх answer → переиспользование фрейма, глубина остаётся 1; safety поверх фрейма → `PREEMPTED`, фрейм восстановлен после clear; `answer_max_s` → принудительный pop |
| `test_tour_flow.py` | полный тур на 3 остановки; рестарт с `start_index=1`; `nav_failed` → skip; отмена `RunTour` в каждом состоянии (параметризовано); второй `RunTour` → `REJECT`; `narrate=false` |
| `test_transit_narration.py` | прибытие раньше конца транзитной реплики → `MODE_SOFT`, экспозиционный нарратив стартует после текущего чанка и **не раньше**; транзитный `Narrate` завершается с пустым `resume_token` (`DROPPABLE`); `eta < transit_min_eta_s` → транзит не запускается; barge-in на ходу при `pause_nav_on_barge_in=true` → nav отменён, после ответа goal переотправлен с тем же pose; при `false` → nav goal не трогается; транзитная реплика длиннее перегона не блокирует прибытие |
| `test_ask_user.py` | ответ распознан по синониму; таймаут во всех трёх режимах `on_timeout`; повтор вопроса; отмена goal клиентом |
| `test_presence.py` | взведение по каждому источнику; снятие ровно через 120 с виртуальных; VAD во время `speaking` игнорируется при `ignore_vad_while_speaking=true` и учитывается при `false`; отсутствие топика `/perception/people` не ломает узел |
| `test_safety_hold.py` | `SAFETY_HOLD` в `NARRATING` → жёсткий стоп + токен; `SAFETY_CLEAR` → возобновление с мостовой фразой; `held_max_s` → `RETURNING` |

**Ассерт, который должен быть в каждом тесте с прерыванием:** после `MODE_HARD` мок `Say` не получает **ни одного** нового goal со старым epoch/scope, и число «произнесённых» символов равно тому, что вернулось в `spoken_text`. Это тот же инвариант, что и epoch-fencing в voice, проверенный на уровне миссии.

### 9.3 Гейт

`test_narration_resume.py` и `test_interrupt_stack.py` — блокирующие для мержа. Пакет считается готовым, когда полный тур с прерыванием на каждом чанке проходит зелёным **до** появления `guide_robot_llm`.

---

## 10. Интеграция с супервизором

- Все три узла — `LifecycleNode`. Порядок в bring-up: после `voice`, до `llm`.
  `presence_monitor` → `narration_server` → `mission_fsm`.
- `configure`: создать клиенты/серверы, прочитать параметры, **не** активировать action-серверы (`RunTour`/`AskUser`/`Narrate` отклоняют goal-ы вне `active`).
- `activate`: запустить поток FSM в `IDLE`, начать публикацию `MissionState`.
- `deactivate`: отменить `RunTour`, `Narrate` MODE_HARD, `NavigateToPose` cancel, опубликовать `IDLE`, остановить поток. Должно укладываться в 2 с.
- Ватчдоги супервизора: `TopicRateWatchdog` на `/mission/state` (1 Гц heartbeat) и `/mission/presence`; `NodeAliveWatchdog` на все три узла.
- `YasminViewerPub` включается параметром `enable_yasmin_viewer` (дефолт `true` на ноутбуке, `false` на Orin).

---

## 11. CLI

```bash
ros2 run guide_robot_mission mission_cli tour --tour main --no-confirm
ros2 run guide_robot_mission mission_cli status          # печатает MissionState, следит
ros2 run guide_robot_mission mission_cli pause [--hard]
ros2 run guide_robot_mission mission_cli resume
ros2 run guide_robot_mission mission_cli ask "Идём дальше?" --options yes,no
ros2 run guide_robot_mission mission_cli say "текст"      # прямой Narrate, без тура
ros2 run guide_robot_mission mission_cli barge            # синтетический barge-in для ручной отладки
```

`barge` публикует в `/speech/barge_in` — позволяет отлаживать весь путь прерывания до появления VAD и до приезда микрофонного массива.

---

## 12. Порядок реализации

1. Интерфейсы в `guide_robot_msgs` (§2) + правка `Say.action` result, если полей нет. Собрать, проверить генерацию.
2. `resume.py`, `chunk_plan.py`, `interrupt_stack.py` + их юнит-тесты. Без ROS. Зелёный прогон здесь — фундамент всего остального.
3. Моки (§9.1) + `harness.py` + `sim_clock.py`.
4. `narration_server` + `test_narration_resume.py` (полная параметризация). До FSM.
5. `presence_monitor` + `test_presence.py`.
6. `mission_fsm`: блэкборд, `InterruptibleState`, `IDLE`/`NARRATING`/`ANSWERING` — минимальный контур прерывания, `test_interrupt_stack.py`.
7. Дополнить FSM `NAVIGATING`/`GREETING`/`AWAITING_CONFIRM`/`RETURNING`/`PAUSED`/`HELD`, `test_tour_flow.py`, `test_safety_hold.py`.
8. `cli.py`, `mission.launch.py`, интеграция в `guide_robot_bringup`, регистрация в супервизоре.

Шаги 4 и 5 независимы и параллелятся.

---

## 13. Что осознанно **не** делается в v1

- Стек глубины > 1. Отказ озвучен пользователю фразой, это продуктовое решение, а не заглушка.
- Возобновление с середины предложения (`char_off` используется только для отчётности).
- Preempt `Narrate` и `RunTour` — только reject.
- `nav2_waypoint_follower` — не даёт корректно прервать и возобновить.
- Мультиязычность: `lang` прокидывается в `GetExhibitContent`, но фразы только `phrases_ru.yaml`.
- Персистентность тура между перезапусками узла.
