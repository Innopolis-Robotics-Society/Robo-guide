# `guide_robot_llm` — Stage 0: голосовой чат с LLM

Техзадание на реализацию. ROS 2 Humble, `ament_python`.

## 1. Цель и рамки

Замкнуть петлю **`/asr/transcript` → LLM → `say`** поверх готового контракта
`guide_robot_voice`, с корректной обработкой отмены (`/speech/cancel_all`) и
честным учётом того, что было реально произнесено.

**Входит в Stage 0:**
- одна lifecycle-нода `chat_node`;
- бэкенды LLM за общим интерфейсом (llama.cpp / OpenAI-совместимый / echo-мок);
- стриминг генерации с разбиением на клаузы и отправкой нескольких `say`-целей;
- epoch-фенсинг хода, усечение истории по фактически произнесённому тексту;
- построчный лог ходов (jsonl);
- юнит-тесты на `lib/` без ROS, без моделей, без звуковой карты.

**Не входит (Stage 1+, не реализовывать, не закладывать заглушки в код):**
RAI, ReAct-цикл, tool registry, снапшот зоны и KB, GBNF-грамматика,
`guardrail`, `interaction_log` с синхронизацией с rosbag, `dialog_agent`.

**Инвариант проекта:** пакет должен быть выключаемым. Ни одна другая нода не
обязана его существованием; `mission_fsm` водит туры без него. `chat_node`
никогда не публикует `/speech/cancel_all` — изоляция L1-контура сохраняется.

## 2. Соглашения проекта

- Пакет создаётся через `ros2 pkg create --build-type ament_python
  guide_robot_llm`. Из `test/` удалить `test_pep257.py` и `test_flake8.py`,
  оставить `test_copyright.py`.
- Линт — `ruff` по корневому `pyproject.toml` монорепозитория.
- Всё в `guide_robot_llm/lib/` — чистая логика **без импорта `rclpy`**.
  Единственное исключение — `lib/qos.py` (как в `guide_robot_voice`).
- Нода — `rclpy.lifecycle.LifecycleNode`. Порядок подъёма принадлежит
  супервизору; в launch-файле `autostart` — опциональный аргумент.
- В `rclpy` подписки жизненным циклом **не** управляются: создавать их в
  `on_configure`, но обрабатывать сообщения только при `self._active is True`,
  флаг выставлять в `on_activate`/`on_deactivate`.

## 3. Дерево файлов

```
guide_robot_llm/
├── package.xml
├── setup.py
├── setup.cfg
├── guide_robot_llm/
│   ├── __init__.py
│   ├── chat_node.py
│   └── lib/
│       ├── __init__.py
│       ├── backends.py
│       ├── history.py
│       ├── sentence_splitter.py
│       ├── turn_log.py
│       └── qos.py
├── config/
│   ├── llm.yaml
│   └── system_prompt.txt
├── launch/
│   └── chat.launch.py
└── test/
    ├── test_copyright.py
    ├── test_backends.py
    ├── test_history.py
    ├── test_sentence_splitter.py
    └── test_turn_log.py
```

## 4. Контракт ROS

| Топик / интерфейс | Тип | QoS | Роль `chat_node` |
|---|---|---|---|
| `/asr/transcript` | `Transcript` | RELIABLE d10 | sub — триггер хода |
| `/speech/wakeword` | `Wakeword` | RELIABLE d1 | sub — гейт вовлечённости |
| `/speech/cancel_all` | `CancelAll` | RELIABLE d1 | sub — abort хода |
| `/voice/speaking` | `SpeakingStatus` | RELIABLE, TRANSIENT_LOCAL d1 | sub — диагностика, эхо-гейт |
| `say` | `Say.action` | — | action client |
| `/diagnostics` | `DiagnosticArray` | стандартный | pub, 1 Гц |
| `/system_event` | `SystemEvent` | RELIABLE d10 | pub — ошибки бэкенда |

> **Проверить перед кодированием:** точные имена полей `Transcript`,
> `Wakeword`, `CancelAll`, `SpeakingStatus`, `Say.action` и набор констант
> `SCOPE_*` / `REASON_*` — по исходникам `guide_robot_msgs`, а не по этому
> документу. В спецификации ниже поля названы по смыслу.

`lib/qos.py` — реэкспорт профилей из `guide_robot_voice.lib.qos`, если пакет
доступен как зависимость; иначе локальное дублирование тех же профилей.
Дублировать значения «на глаз» нельзя — QoS должен совпадать с издателем.

## 5. `chat_node`: поведение

### 5.1 Гейты приёма транскрипта

Транскрипт принимается к обработке, только если выполнено всё:

1. нода `active`;
2. `msg.is_final == true`;
3. длина текста после очистки ≥ `min_chars`;
4. **гейт вовлечённости**: `require_wakeword == false`, ИЛИ с момента
   последнего `/speech/wakeword` прошло меньше `engagement_timeout_s`.
   Таймер вовлечённости обновляется при каждом принятом транскрипте и при
   каждом wakeword;
5. **эхо-гейт**: `echo_guard_ms > 0` И интервал высказывания пересекается с
   окном `speaking=true` + хвост `echo_guard_ms` → отбросить.
   *На гарнитуре механизм не нужен, поэтому дефолт `echo_guard_ms: 0.0`.
   Параметр и код обязаны существовать: с появлением колонок без AEC это
   единственная защита от самоподслушивания;*
6. нет активного хода. Если ход активен — `WARN` в лог, транскрипт
   отбрасывается. Очередь ходов не делаем.

Если `strip_activation_phrase == true`, из начала текста вырезается совпавшая
активационная фраза. Wakeword детектится по `/asr/partial`, поэтому финальный
транскрипт содержит её целиком — без вырезания модель каждый ход получает
«робот, …» в префиксе. Список фраз — параметр `activation_phrases`, должен
совпадать со списком `wakeword_node`; сравнение регистронезависимое, с
допуском на пунктуацию после фразы.

### 5.2 Жизненный цикл хода

```
IDLE ──accept──► GENERATING ──┬──► SPEAKING ──► finalize ──► IDLE
                              │      ▲   │
                              └──────┘   │        (стрим и озвучка
                                         │         перекрываются)
                    CancelAll ───────────┴──► drain ──► finalize(interrupted)
```

Объект `Turn`:

| Поле | Смысл |
|---|---|
| `turn_id` | монотонный счётчик |
| `epoch` | `node.get_clock().now().nanoseconds` на момент приёма транскрипта |
| `user_text` | очищенный транскрипт |
| `generated` | полный текст, отданный моделью |
| `clauses` | список отправленных клауз, по порядку |
| `goals` | хендлы `say`-целей, индекс = индекс клаузы |
| `spoken` | `spoken_text` из результатов, по индексу |
| `interrupted` | флаг |

Шаги:

1. Принять транскрипт, создать `Turn`, `epoch = now`.
2. Собрать сообщения: `system_prompt` + `history.window()` + user-реплика.
3. Запустить worker-поток генерации, токены складывать в `queue.Queue`.
4. Таймер 20 Гц дренирует очередь и кормит `SentenceSplitter`. На каждую
   готовую клаузу — `send_goal_async` на `say`.
5. По завершении генерации — `splitter.flush()`, последняя клауза.
6. Результаты `say` собираются по индексу, `spoken_text` копится.
7. Ход финализируется, когда генерация завершена **и** все отправленные цели
   разрешены (SUCCEEDED / CANCELED / ABORTED / REJECTED).
8. В историю кладётся ассистентская реплика, собранная из `spoken`, а не из
   `generated`. Если хоть одна цель не SUCCEEDED — добавить маркер
   `interrupted_marker` в конец. Строка `turn_log` пишется, состояние → IDLE.

### 5.3 Отмена

Колбэк `/speech/cancel_all` держать коротким: выставить
`last_cancel_epoch = msg.epoch`, `abort_event.set()`, поставить задачу отмены
целей в исполнитель. Никакого ожидания, никакого HTTP в колбэке.

Правила фенсинга:

- **новые** клаузы не отправляются, если `turn.epoch < last_cancel_epoch`;
- токены, пришедшие от прерванного worker'а, выбрасываются молча;
- **результаты уже отправленных целей выбрасывать нельзя** — именно из них
  берётся `spoken_text`. Ход после отмены переходит в «дренаж»: новые
  эмиссии заблокированы, сбор результатов продолжается до финализации.
  Это главная тонкость реализации, не сокращать.
- цели отменяются через `cancel_goal_async` на всех неразрешённых хендлах.
  `tts_node` погасит и очередь, и активную цель сам; клиентская отмена нужна,
  чтобы не остались висеть цели, ещё не принятые сервером;
- реагировать на любой `CancelAll`, чей `scope` покрывает диалог. Проверить
  константы; при сомнении — реагировать на всё.

### 5.4 Параметры `say`-цели

`priority = say_priority` (ниже mission и safety), `interruptible = true`,
`scope = say_scope`, `max_duration = say_max_duration_s`, `voice = ""`.

Если сервер отклонил цель (переполнение очереди `tts_node`, `max_queue=8`) —
удержать клаузу в клиентском буфере и повторить отправку по разрешению
предыдущей цели, не более `goal_retry_limit` раз. При исчерпании — `WARN`,
клауза теряется, ход продолжается.

**Порядок клауз** опирается на FIFO очереди `tts_node` при равных
`priority`/`scope`. Проверить на этапе 2 экспериментально; если порядок
нарушается — перейти на клиентскую последовательную отправку (цель N+1 после
принятия цели N, не после результата — иначе появится слышимая пауза).

### 5.5 Отказы и деградация

| Ситуация | Реакция |
|---|---|
| нет первого токена за `first_token_timeout_s` | abort, канонная фраза через `say`, `SystemEvent(ERROR, id="llm.timeout")` |
| исключение/обрыв соединения до первой клаузы | канонная фраза, `SystemEvent(ERROR, id="llm.backend")` |
| обрыв после того, как что-то уже озвучено | канонную фразу **не** говорим, ход финализируется как `interrupted` |
| `request_timeout_s` на полном ответе | abort, ход финализируется по произнесённому |

Канонные фразы — `fallback_phrases`, выбор циклический, не случайный
(воспроизводимость в тестах). Нода при любом отказе остаётся `active`.

## 6. Модули `lib/`

### `backends.py`

```python
@dataclass
class Chunk:
    text: str
    done: bool

class LlmBackend(Protocol):
    def stream(self, messages: list[dict], abort: threading.Event) -> Iterator[Chunk]: ...
    def health(self) -> tuple[bool, str]: ...
```

- `LlamaCppBackend` — `POST {base_url}/v1/chat/completions`, `stream=true`,
  разбор SSE построчно (`data: ` … `data: [DONE]`). Проверять `abort` между
  строками, соединение закрывать явно. `health()` — `GET {base_url}/health`
  или `/v1/models`.
- `OpenAIBackend` — тот же протокол, отличается заголовком авторизации и
  обязательным `model`. Ключ читается из переменной окружения, **не** из yaml.
- `EchoBackend` — детерминированный ответ без сети: отдаёт заранее заданный
  текст по-словно с настраиваемой задержкой. Аналог `NullBackend` в
  `guide_robot_voice`. Нужен для CI и для этапа 0.

HTTP-клиент — `requests` (rosdep-ключ `python3-requests` существует).
`llama_ros` намеренно не используется: плоский HTTP одинаково работает с
локальной llama.cpp, машиной в LAN и внешним API, поэтому цепочка деградации
«внешняя → локальная → канонные фразы» становится вопросом конфига.

### `history.py`

Кольцо диалоговых пар с усечением по `max_history_turns`. Метод
`append_turn(user_text, spoken_text, interrupted: bool)` — в историю
попадает **произнесённое**, с маркером при прерывании. Метод `window()`
возвращает список `{"role", "content"}` без системного промпта.

### `sentence_splitter.py`

Инкрементальный: `feed(text) -> list[str]`, `flush() -> str | None`.
Границы — `.!?…` и `;` с последующим пробелом/концом, плюс принудительный
разрез по `max_clause_chars`. Не резать по точке внутри сокращений и чисел —
переиспользовать правила из `guide_robot_voice/lib/text_chunker.py`, если
модуль импортируем; иначе продублировать список русских сокращений.
Первую клаузу разрешено отдавать короче (`first_clause_min_chars`) ради TTFA.

### `turn_log.py`

jsonl, один файл на сессию, `log_dir/chat_YYYYmmdd_HHMMSS.jsonl`, запись
построчно с `flush()`. Схема строки:

```json
{
  "ts": 1730000000.123, "turn_id": 7, "epoch": 1730000000123456789,
  "user_text": "...", "asr_confidence": -1.0, "wakeword": "робот",
  "history_turns": 4, "generated": "...", "spoken": "...",
  "clauses": ["...", "..."], "goal_statuses": ["SUCCEEDED", "CANCELED"],
  "ttft_ms": 480.2, "gen_ms": 2130.7, "speak_ms": 5400.0,
  "interrupted": true,
  "cancel": {"reason": "barge_in", "epoch": 1730000002000000000, "latency_ms": 142.0},
  "backend": "llama_cpp", "model": "qwen2.5-3b-instruct-q4_k_m"
}
```

## 7. Параметры (`config/llm.yaml`)

```yaml
chat_node:
  ros__parameters:
    backend: "llama_cpp"          # llama_cpp | openai | echo
    base_url: "http://127.0.0.1:8080"
    model: ""
    api_key_env: "LLM_API_KEY"
    system_prompt_file: ""        # пусто -> config/system_prompt.txt
    max_history_turns: 6
    max_tokens: 256
    temperature: 0.7
    stream: true
    first_token_timeout_s: 3.0
    request_timeout_s: 20.0

    require_wakeword: true
    activation_phrases: ["робот", "слушай робот"]
    strip_activation_phrase: true
    engagement_timeout_s: 120.0
    min_chars: 2
    echo_guard_ms: 0.0            # >0 только когда появятся колонки без AEC

    say_priority: 40
    say_scope: 0                  # свериться с константами SCOPE_*
    say_max_duration_s: 30.0
    goal_retry_limit: 3
    max_clause_chars: 180
    first_clause_min_chars: 24

    fallback_phrases: ["Извините, сейчас не могу ответить."]
    interrupted_marker: " [прервано]"
    log_dir: "~/.ros/llm_turns"
    diagnostics_hz: 1.0
```

`engagement_timeout_s: 120.0` — из исследования музейного робота Alter-Ego,
не менять без причины.

Пути к файлам разворачиваются через `$(find-pkg-share guide_robot_llm)`,
поэтому launch обязан оборачивать `parameters` в
`launch_ros.parameter_descriptions.ParameterFile(..., allow_substs=True)` —
те же грабли, что в `guide_robot_voice`.

## 8. Модель исполнения

- `MultiThreadedExecutor`, `ReentrantCallbackGroup` для action-клиента и
  таймеров, отдельная `MutuallyExclusiveCallbackGroup` для подписок.
- Генерация — в `threading.Thread`, общение с нодой только через
  `queue.Queue` и `threading.Event`. Никаких вызовов `rclpy` из worker'а.
- Все переходы состояния `Turn` выполняются в дренирующем таймере и колбэках
  результатов, то есть в одном логическом потоке. Блокировка — один
  `threading.Lock` на объект хода.

## 9. Lifecycle

| Переход | Действия |
|---|---|
| `on_configure` | прочитать параметры, загрузить системный промпт, создать бэкенд, `health()` (провал → `FAILURE`), создать подписки/паблишеры/action-клиента, открыть файл лога |
| `on_activate` | `self._active = True`, запустить таймеры |
| `on_deactivate` | `self._active = False`, отменить активный ход, остановить таймеры |
| `on_cleanup` | закрыть бэкенд и файл лога, снести интерфейсы |
| `on_shutdown` | как cleanup, дождаться worker-потока с таймаутом |

## 10. Диагностика

`/diagnostics`, 1 Гц, `hardware_id="chat_node"`, ключи: `backend`, `model`,
`state` (`idle`/`generating`/`speaking`/`draining`), `turns_total`,
`turns_interrupted`, `last_ttft_ms`, `last_total_ms`, `backend_ok`,
`last_error`. Уровень `ERROR` при недоступном бэкенде, `WARN` при
`turns_interrupted / turns_total > 0.5` на последних 10 ходах.

## 11. Launch

`chat.launch.py`, аргументы: `params_file` (дефолт `config/llm.yaml`),
`autostart` (дефолт `false`), `log_level`. При `autostart:=true` — стандартный
`nav2_lifecycle_manager` с **`bond_timeout: 0.0`** (наши ноды bond не
создают — те же грабли, что в voice) или простой `LifecycleTransition`.
Отдельного launch, поднимающего voice+llm вместе, в Stage 0 не делаем:
`voice.launch.py` и `chat.launch.py` запускаются рядом.

## 12. Тесты

Юнит-тесты без ROS, без сети, без моделей:

- `test_sentence_splitter.py` — границы клауз, сокращения, числа с точкой,
  принудительный разрез, `flush()` остатка, `first_clause_min_chars`.
- `test_history.py` — окно, вытеснение, в историю попадает `spoken`, а не
  `generated`; маркер прерывания.
- `test_backends.py` — `EchoBackend`: стрим, реакция на `abort` в середине;
  парсер SSE `LlamaCppBackend` на фикстурных байтах (без сокета).
- `test_turn_log.py` — схема строки, устойчивость к незакрытому файлу.

Запуск: `python3 -m pytest test/ -q` из каталога пакета. При проблемах с
автозагрузкой плагинов — `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

Ноды юнитами не покрываются — верификация по этапам ниже, на живом стеке.

## 13. Этапы приёмки

| Этап | Состав | Критерий |
|---|---|---|
| 0 | скелет, `backend: echo`, `require_wakeword: false`, `stream: false` | ручной `ros2 topic pub /asr/transcript` → робот проговаривает эхо-ответ |
| 1 | `LlamaCppBackend`, non-stream | тот же pub → осмысленный ответ; `ttft_ms`/`gen_ms` в `turn_log` |
| 2 | стрим + splitter, N целей `say` | TTFA < 1.5 с на Qwen2.5-3B; порядок клауз не нарушен, слышимых пауз между клаузами нет |
| 3 | живой `voice.launch.py` на гарнитуре, `require_wakeword: true` | «робот, расскажи что-нибудь» → ответ; без активационной фразы — тишина; активационная фраза не уходит в промпт |
| 4 | стоп-слово во время ответа | TTS замолкает; в `turn_log` `spoken` короче `generated`; следующий ход в промпте содержит только произнесённое, с маркером |
| 5 | обрыв llama-server, таймауты | канонная фраза, нода жива и `active`, `/diagnostics` в `ERROR`, восстановление после подъёма сервера без рестарта ноды |

Этап 4 — целевой. Он проверяет `epoch`/`spoken_text` end-to-end до того, как
сверху ляжет mission FSM; всё остальное — подводка к нему.

## 14. Что переедет в `dialog_agent`

`lib/` целиком, гейт вовлечённости, epoch-фенсинг хода, усечение истории по
`spoken_text`, `turn_log` (станет `interaction_log` с синхронизацией с
rosbag). Добавится: tool registry поверх actions/srvs, сборка снапшота зоны,
пересборка промпта на границе зоны, GBNF/JSON-schema как первичное
ограничение вывода, нода `guardrail` как вторичное. `chat_node` остаётся
в пакете навсегда как отладочный инструмент «LLM без тура».
