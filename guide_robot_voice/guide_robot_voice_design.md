# guide_robot_voice — детальный проект пакета

`ament_python`, ROS 2 Humble. Чистый аудио-I/O: ничего не знает о туре, экспонатах и LLM.
Единственная семантика, которую пакет понимает, — приоритет, scope и epoch.

---

## 0. Расхождения с исходным ТЗ

Проверено против `guide_robot_msgs` (распакованный архив). Восемь пунктов, где проект
отличается от текста ТЗ; каждое отличие обосновано.

| # | ТЗ | Проект | Причина |
|---|----|--------|---------|
| 1 | `/speech/cancel_all` = `std_msgs/Empty` | `guide_robot_msgs/CancelAll` | epoch-fencing, scope, reason — иначе гонка «чанк синтезирован за 3 мс до отмены» |
| 2 | `/voice/is_speaking` = `Bool` latched | `SpeakingStatus` @ 5 Гц + TRANSIENT_LOCAL | latched Bool при падении ноды навсегда застревает в `true` |
| 3 | `/speech/wakeword` = `String` | `Wakeword` | без `confidence` и `tts_active` не считается false-wake-under-TTS — приёмочная метрика |
| 4 | один `/asr/transcript` | `/asr/partial` (BEST_EFFORT d1) + `/asr/transcript` (RELIABLE d10) | партиалы на RELIABLE d10 при 6 Гц забивают очередь и тормозят финалы |
| 5 | ASR = faster-whisper / WhisperTRT | GigaAM v3 CTC через sherpa-onnx | Whisper не потоковый by design (окно 30 с), RU-качество у small/medium слабое, large-v3 не влезает рядом с LLM в 8 ГБ. GigaAM — RU-специализированный CTC, честный стриминг, INT8 |
| 6 | wakeword = openWakeWord | Stage 1: KWS поверх партиалов ASR; Stage 3: openWakeWord с собственной моделью | у openWakeWord нет готовых русских моделей; своя модель обучается на синтетике Piper — это отдельная работа, не блокирующая barge-in |
| 7 | `turn_detector` — отдельная нода | библиотека внутри `asr_node` | своего типа сообщения для end-of-turn нет; `is_final=true` в `Transcript` **и есть** сигнал конца хода. Выносить в ноду — когда появится семантическая модель |
| 8 | `epoch` — «счётчик, инкрементируется отправителем» | `epoch = monotonic ns` на момент публикации | **дефект контракта**: см. ниже |

### 0.1 Дефект контракта epoch (требует правки в `CancelAll.msg`)

Издателей `CancelAll` несколько: `vad_node` (barge-in), `wakeword_node` (стоп-слово),
`supervisor` (estop), `mission` (nav_event). Если каждый ведёт **свой** счётчик,
приёмник, хранящий `max(epoch)`, отбросит легитимную отмену:

```
vad_node       публикует epoch=1  -> sink запоминает 1
wakeword_node  публикует epoch=1  -> sink отбрасывает (1 !< 1) -> робот не заткнулся
```

Правка: `epoch` — любое монотонно неубывающее значение из **общего** источника.
Практически: `epoch = self.get_clock().now().nanoseconds`. Координация не нужна,
брокер на пути безопасности не нужен, все ноды на одном хосте (Orin) → один
`CLOCK_REALTIME`. Ограничение: при разъезде по машинам требуется PTP/NTP — в
комментарии сообщения зафиксировать.

Альтернатива (отвергнута): выделенный `cancel_broker`. Добавляет хоп ~2 мс и
единую точку отказа на пути L1-безопасности.

---

## 1. Топология процессов

```
Stage 1-2 (сейчас, Python, отладка)          Целевая (Stage 3, C++)
─────────────────────────────────           ──────────────────────
[audio_frontend] --AudioChunk topic-->      ┌──────── audio_pipeline (C++) ───────┐
[vad_node]                                  │ capture -> ring -> VAD -> WW        │
[wakeword_node]                             │ (intra-process, без сериализации)   │
[asr_node]                                  └── /vad /speech/wakeword /asr/* ─────┘
[tts_node]                                  [asr_node] [tts_node]
```

Топик `AudioChunk` — временный транспорт периода разработки (это уже записано в
самом `.msg`). Внешний контракт (`/asr/*`, `/speech/*`, `/voice/*`, action `say`)
при переезде не меняется — это и есть критерий, что границы проведены правильно.

Почему сейчас отдельные процессы: rclpy не поддерживает intra-process comms вообще,
так что выигрыша от объединения в Python нет, а раздельные ноды дают независимый
lifecycle и `ros2 topic hz` на каждом стыке. Цена — сериализация `int16[]`:
256 сэмплов × 62.5 Гц ≈ 32 КБ/с, на loopback DDS шум.

**Исключение из этого правила появится на Stage 2**: софтовый AEC требует, чтобы
захват и воспроизведение шли с **одного** устройства и одного тактового генератора,
то есть `audio_frontend` и sink `tts_node` сливаются в один процесс `audio_io`.
См. §7.

---

## 2. Контракт пакета

### Публикации

| Топик | Тип | QoS | Частота |
|-------|-----|-----|---------|
| `/audio/mic` | `AudioChunk` | BEST_EFFORT, d5 | 62.5 Гц (16 мс) |
| `/vad` | `VoiceActivity` | BEST_EFFORT, d1 | 31.25 Гц (32 мс) |
| `/speech/wakeword` | `Wakeword` | RELIABLE, d1 | по событию |
| `/asr/partial` | `Transcript` | BEST_EFFORT, d1 | 5–10 Гц |
| `/asr/transcript` | `Transcript` | RELIABLE, d10 | по событию |
| `/voice/speaking` | `SpeakingStatus` | RELIABLE, TRANSIENT_LOCAL, d1 | 5 Гц heartbeat + по изменению |
| `/speech/cancel_all` | `CancelAll` | RELIABLE, d1 | по событию (издатели: vad, wakeword) |
| `/diagnostics` | `DiagnosticArray` | стандартный | 1 Гц |
| `/system_event` | `SystemEvent` | RELIABLE, d10 | по событию |

### Подписки

| Топик | Тип | Кто слушает |
|-------|-----|-------------|
| `/speech/cancel_all` | `CancelAll` | `tts_node` |
| `/audio/mic` | `AudioChunk` | `vad_node`, `wakeword_node`, `asr_node` |
| `/vad` | `VoiceActivity` | `asr_node`, `vad_node`→ own barge-in logic |
| `/voice/speaking` | `SpeakingStatus` | `wakeword_node` (заполнить `tts_active`), `vad_node` (гейт barge-in) |

### Серверы

| Имя | Тип | Нода |
|-----|-----|------|
| `say` | `Say.action` | `tts_node` |

`/voice/speaking` — TRANSIENT_LOCAL, чтобы поздно поднявшийся `mission` сразу узнал
состояние. Потребитель обязан считать статус протухшим при `now - stamp > 400 мс`
(два периода heartbeat) — это уже записано в `.msg`.

### Именование

Все топики в глобальном пространстве, без namespace. Причина: `collision_monitor`,
supervisor и mission ссылаются на них абсолютно, а голосовой стек в одном
экземпляре. Ремапы — на уровне launch, если понадобится второй робот.

---

## 3. Ноды

Все ноды — `LifecycleNode`. Порядок bring-up owned супервизором:
`tts_node` → `audio_frontend` → `vad_node` → `wakeword_node` → `asr_node`.
Причина порядка: TTS должен уметь сказать «инициализация» до того, как поднят вход;
микрофонная цепочка активируется последней, чтобы не ловить собственные тестовые тоны.

### 3.1 `audio_frontend`

Единственный владелец устройства захвата. Больше никто в системе не открывает PCM на вход.

**Ответственность**
- Открыть PCM (`sounddevice`/PortAudio, `hw:` напрямую), фиксированный размер периода.
- Ресемплинг 48000 → 16000 (целочисленное ÷3, polyphase FIR через `soxr`/`resample_poly`).
- Downmix в моно (Stage 1 — тривиально; Stage 3 — берётся beamformed-канал XVF3800).
- DC-blocker (HPF 1-го порядка, fc=40 Гц) + опциональный фиксированный gain. **AGC выключен**: он ломает и AEC, и стабильность порогов VAD.
- Штамп времени = момент **первого** сэмпла кадра, вычисляемый как `now - (frames_in_buffer / rate)`, а не `now`. Иначе весь бюджет barge-in измеряется неправильно.
- Монотонный `first_sample`; при xrun — не «залатать нулями», а сделать разрыв в `first_sample` и залогировать `SystemEvent(severity=ERROR, id="audio.xrun")`.
- Stage 2/3: AEC (см. §7).

**Кадр: 16 мс = 256 сэмплов @16 кГц.** Число не произвольное:
- Silero VAD v5 требует ровно 512 сэмплов → **2 кадра**;
- openWakeWord работает окнами по 1280 сэмплов (80 мс) → **5 кадров**;
- на 48 кГц это 768 сэмплов на период — валидный размер для ALSA.

При 10 или 32 мс одно из двух условий ломается и появляется буфер-склейка с
неопределённой задержкой.

**Параметры**
```yaml
audio_frontend:
  ros__parameters:
    device: "hw:2,0"          # WJK / гарнитура; Stage 3 -> XVF3800
    device_rate: 48000
    out_rate: 16000
    frame_ms: 16
    channels_in: 1
    periods: 3                # 48 мс буфера захвата
    gain_db: 0.0
    hpf_hz: 40.0
    publish_raw: false        # диагностический дубль на device_rate
    aec:
      enabled: false          # Stage 2+
      backend: "none"         # none | speexdsp | webrtc | hardware
      filter_length_ms: 200
```

**Lifecycle**: `configure` — открыть устройство, проверить, что запрошенный формат
принят без подмены (PortAudio молча даёт resample — проверять `samplerate` фактический);
`activate` — старт стрима и публикации; `deactivate` — стоп стрима, устройство держим
открытым (переоткрытие ALSA ~200 мс); `cleanup` — закрыть.

### 3.2 `vad_node`

**Ответственность**
- Silero VAD v5 (ONNX, CPU) поверх окон 512 сэмплов. Резерв: TEN VAD.
- Гистерезис: вход в речь по `p > enter_threshold` **N** окон подряд, выход — по
  `p < exit_threshold` в течение `hangover_ms`. Два порога, не один — иначе дребезг
  на границе.
- Публикация `VoiceActivity` каждое окно, включая `level_dbfs` (нужен, чтобы отличить
  «VAD молчит» от «микрофон не тот / уровень в пол»).
- **Barge-in**: при переходе в речь, если `SpeakingStatus.speaking == true` и не
  выключено параметром — публикует `CancelAll(scope=SCOPE_ALL, reason=REASON_BARGE_IN)`.

Barge-in живёт здесь, а не в mission/LLM, сознательно: это L1-путь, он обязан работать
при мёртвом LLM. Задержка = один хоп DDS.

**Ключевой нюанс Stage 1 vs Stage 3**: на гарнитуре акустической связи «динамик → микрофон»
нет, поэтому VAD во время TTS чист и barge-in работает без AEC вообще. На роботе с
громкоговорителем без AEC он сработает на первом же слове самого робота. Отсюда параметр
`require_aec_for_barge_in`: на Stage 3 barge-in автоматически запрещается, если фронтенд
не рапортует активный AEC. Это защита от сценария «переехали на железо, забыли включить
AEC, робот перебивает сам себя».

```yaml
vad_node:
  ros__parameters:
    model_path: "$(find-pkg-share guide_robot_voice)/models/silero_vad.onnx"
    enter_threshold: 0.65
    exit_threshold: 0.35
    enter_windows: 2          # 64 мс подтверждения
    hangover_ms: 400
    min_speech_ms: 120        # короче -> не высказывание, а хлопок
    barge_in_enabled: true
    barge_in_min_windows: 2
    require_aec_for_barge_in: false   # -> true на Stage 3
```

`hangover_ms=400` — это же значение служит базой для end-of-turn (§3.4).

### 3.3 `wakeword_node`

Два бэкенда за одним интерфейсом, выбираются параметром.

- `backend: asr_kws` (Stage 1) — подписка на `/asr/partial`, нормализация (lowercase,
  ё→е, удаление пунктуации), нечёткое сравнение по расстоянию Левенштейна с фразами
  из конфига. Даёт «стоп-слово» бесплатно, ценой задержки ASR (~300–500 мс) и
  зависимости от работающего ASR.
- `backend: oww` (Stage 3) — openWakeWord, окна 1280 сэмплов, своя модель, обученная
  на синтетике Piper (`ru_RU-irina-medium` + аугментация RIR/шумом зала). Задержка
  ~100 мс, работает при выключенном ASR.

Публикует `Wakeword` с `tts_active`, взятым из последнего `SpeakingStatus` (протухший
статус → `tts_active=false` + WARN в диагностику).

**Стоп-слова** — отдельный список. При срабатывании нода **сама** публикует
`CancelAll(scope=SCOPE_ALL, reason=REASON_WAKEWORD)`, не дожидаясь mission. Это L1.

```yaml
wakeword_node:
  ros__parameters:
    backend: "asr_kws"
    activation_phrases: ["робот", "слушай робот"]
    stop_phrases: ["стоп", "стой", "хватит", "замолчи"]
    fuzzy_max_distance: 1
    min_confidence: 0.5
    refractory_ms: 1500       # анти-дребезг, одно срабатывание на фразу
```

### 3.4 `asr_node`

GigaAM v3 CTC через sherpa-onnx (`OnlineRecognizer`), INT8, greedy. Beam search и
внешняя LM — потом, если WER потребует.

**Поток**
1. Кадры `/audio/mic` копятся, пока `/vad` не даст `active=true`.
2. Открывается высказывание: `utterance_id++`, в поток подаётся **pre-roll** —
   `pre_roll_ms` кадров ДО срабатывания VAD из кольцевого буфера. Без этого срезается
   первый слог; 300 мс достаточно.
3. Партиалы публикуются с троттлингом до `partial_rate_hz`, `is_final=false`.
4. Финализация — по политике end-of-turn.
5. `Transcript(is_final=true)` в `/asr/transcript`, `speech_start/speech_end`
   относительно начала высказывания, `confidence` = средний CTC-скор, `language="ru"`,
   `azimuth=NaN` до появления DoA.

**Политика end-of-turn** (библиотека `turn_policy.py`, не нода):
```
finalize если:
    тишина >= base_silence_ms                                   (600 мс)
  ИЛИ тишина >= short_silence_ms И текст синтаксически завершён  (350 мс)
  ИЛИ длительность >= max_utterance_s                            (20 с, страховка)
```
«Синтаксически завершён» на Stage 1 — эвристика: не заканчивается на предлог/союз/
вопросительное слово, длина ≥ 2 слов. Интерфейс политики принимает
`(partial_text, silence_ms, utterance_ms)` и возвращает `bool` — ровно та сигнатура,
которую позже подменит семантическая модель, без изменений в остальном коде.

`azimuth` в `Transcript` заполняется из `/doa` (появится на Stage 3, публикует
`audio_frontend`); поле уже в контракте, чтобы не менять msg позже.

```yaml
asr_node:
  ros__parameters:
    model_dir: ".../gigaam_v3_ctc_int8"
    pre_roll_ms: 300
    partial_rate_hz: 6
    base_silence_ms: 600
    short_silence_ms: 350
    max_utterance_s: 20.0
    min_final_chars: 2
    gate_on_tts: false        # true -> не слушать во время речи робота (Stage 0/1 half-duplex)
```

### 3.5 `tts_node`

Самая объёмная нода. Пять внутренних компонентов, каждый тестируется отдельно.

```
Say.action goal
      |
   Scheduler ──(вытеснение по priority/scope/interruptible)
      |
  TextChunker ──(клаузы, RU-аббревиатуры, числа)
      |
  Piper (warm) ──22050 Hz PCM──> Resampler ──48000 Hz──> EpochFencedSink ──> ALSA
      |                                                        |
      +--> Say feedback (clause_index, progress)               +--> SpeakingStatus
```

**Scheduler.** Одна активная цель + очередь.
- новая цель с `priority > active.priority` **и** `active.interruptible == true` → вытеснение, результат активной = `STATUS_PREEMPTED` с корректным `spoken_text/spoken_chars`;
- `priority <= active.priority` → в очередь (стабильная сортировка по приоритету, затем FIFO);
- очередь переполнена (`max_queue`) → `STATUS_REJECTED`;
- `interruptible == false` защищает от вытеснения по приоритету и от barge-in, **но не** от `CancelAll` с `reason=REASON_ESTOP` или `scope=SCOPE_SAFETY`. Это осознанное различие: «отойдите, робот поворачивается» не должно прерываться посетителем, но обязано прерываться e-stop.

**CancelAll → выбор целей:** отменяются цели, у которых `goal.scope == msg.scope`,
либо `msg.scope == SCOPE_ALL`. Результат — `STATUS_CANCELLED`.

**TextChunker.** Делит текст на клаузы по границам предложений с учётом русских
сокращений (`т.е.`, `см.`, `г.`, `им.`, `ул.`), инициалов, десятичных дробей и
диапазонов. Каждая клауза синтезируется отдельно → это и единица прогресса, и
точка, на которой штатная отмена (`CancelGoal`) останавливается «по-человечески».
Ограничение сверху `max_clause_chars` (≈180), иначе одна клауза даёт секунды
неотменяемого синтеза.

Именно чанкер обеспечивает главное поле результата — `spoken_text`: точный
префикс исходного текста, по которому `narration_server` возобновляет монолог
с места прерывания, а не с начала.

**Resampler.** 22050 → 48000, отношение 320/147, polyphase. Состояние фильтра
переносится между чанками (иначе щелчки на стыках). Сбрасывается только на
`bump()` epoch.

**EpochFencedSink.** Инвариант: после `bump(new_epoch)` в устройство попадает
**не более одного** уже отданного чанка. Достигается тем, что sink пишет блоками
`block_ms=20` и перед каждым блоком сверяет epoch чанка с текущим. Реализация —
callback-режим PortAudio (не блокирующая запись): в callback лежит проверка epoch
и подмена на тишину, что даёт детерминированную верхнюю границу задержки, равную
одному периоду.

При отмене: `bump()` → очистка внутренней очереди → `abort()` PCM (сброс буфера
устройства, **не** `drain()`) → сброс состояния ресемплера → `SpeakingStatus`
с новым epoch и `speaking=false`.

**Piper.** Тёплый процесс, модель `ru_RU-irina-medium` загружается в `configure`
(~0.45 с), в `activate` — прогревочный синтез короткой фразы в /dev/null, чтобы
первая реальная реплика не платила за ленивую инициализацию ONNX-графа.

```yaml
tts_node:
  ros__parameters:
    model_path: ".../ru_RU-irina-medium.onnx"
    speaker_id: 0
    length_scale: 1.0
    device: "hw:2,0"
    device_rate: 48000
    block_ms: 20
    periods: 3                 # 60 мс буфера воспроизведения
    max_clause_chars: 180
    max_queue: 8
    heartbeat_hz: 5.0
    warmup_text: "Система готова"
    default_priority: 50
```

---

## 4. Бюджет задержки barge-in

Требование: < 200 мс от начала речи посетителя до тишины из динамика.

| Этап | мс |
|------|-----|
| Буфер захвата (2 периода по 16 мс) | 32 |
| Окно VAD (512 сэмплов) | 32 |
| Инференс Silero (CPU, Orin) | 2–4 |
| Подтверждение (`enter_windows=2`) | 32 |
| Публикация `CancelAll` + DDS loopback | 2–5 |
| Приём в sink, худший случай — только что начатый блок | 20 |
| `abort()` PCM, сброс буфера устройства | 1–3 |
| **Итого, худший случай** | **~128** |

Запас ~70 мс. Расходуется он в первую очередь на `periods` в воспроизведении: если
поднять буфер вывода до 5 периодов ради борьбы с underrun, `abort()` всё равно
сбрасывает его мгновенно — задержка не растёт. А вот увеличение `block_ms` растёт
в бюджет линейно, поэтому `block_ms=20` — не тюнинг-параметр, а часть инварианта.

**Как измеряется.** Не секундомером. `audio_frontend` штампует кадр моментом первого
сэмпла; sink логирует момент записи последнего ненулевого сэмпла в устройство.
Разница этих двух штампов и есть метрика. Записывается в `SystemEvent` при каждом
barge-in — то есть метрика собирается в проде, а не только на стенде.

---

## 5. Структура пакета

```
guide_robot_voice/
├── package.xml
├── setup.py
├── setup.cfg
├── resource/guide_robot_voice
├── config/
│   ├── voice.yaml               # все ноды, один файл
│   └── voice_headset.yaml       # оверлей Stage 1 (устройства, пороги)
├── launch/
│   ├── voice.launch.py          # весь стек
│   ├── tts_only.launch.py       # отладка выхода
│   └── input_only.launch.py     # отладка входа
├── models/                      # git-lfs: silero_vad.onnx, piper, gigaam
├── guide_robot_voice/
│   ├── __init__.py
│   ├── audio_frontend.py
│   ├── vad_node.py
│   ├── wakeword_node.py
│   ├── asr_node.py
│   ├── tts_node.py
│   └── lib/
│       ├── chunker.py           # TextChunker
│       ├── scheduler.py         # Scheduler
│       ├── sink.py              # EpochFencedSink
│       ├── resampler.py         # Resampler
│       ├── turn_policy.py       # политика end-of-turn
│       ├── ring.py              # кольцевой буфер pre-roll
│       ├── audio_device.py      # обёртка PortAudio + проверка формата
│       └── qos.py               # профили QoS одним местом
└── test/
    ├── test_copyright.py
    ├── test_chunker.py
    ├── test_scheduler.py
    ├── test_sink_epoch.py
    ├── test_resampler.py
    └── test_turn_policy.py
```

`lib/` не импортирует `rclpy` нигде, кроме `qos.py`. Это даёт юниты без запуска ROS
и делает C++-переписывание Stage 3 механическим.

---

## 6. Порядок реализации

Каждый шаг заканчивается работающим на гарнитуре артефактом.

| Шаг | Содержание | Критерий готовности |
|-----|------------|---------------------|
| 1 | `lib/`: chunker, resampler, scheduler, sink + юниты | `colcon test` зелёный, ROS не нужен |
| 2 | `tts_node` + `tts_only.launch.py` | `ros2 action send_goal say` → слышна фраза, feedback идёт, `spoken_text` корректен при `CancelGoal` посреди фразы |
| 3 | Ручная публикация `CancelAll` в топик | замер задержки < 200 мс, никакого «хвоста» после отмены |
| 4 | `audio_frontend` | `ros2 topic hz /audio/mic` = 62.5, `first_sample` без разрывов 10 минут, `level_dbfs` вменяемый |
| 5 | `vad_node` без barge-in | `/vad` реагирует на речь, ложные срабатывания на тишине = 0 за 5 минут |
| 6 | barge-in в `vad_node` | перебиваю робота голосом — замолкает, метрика пишется в `SystemEvent` |
| 7 | `asr_node` + `turn_policy` | партиалы 6 Гц, финал приходит через ~600 мс тишины, WER на 30 фразах |
| 8 | `wakeword_node` (`asr_kws`) | стоп-слово гасит речь, false-wake rate за 30 минут работы |

Шаги 1–3 — половина объёма пакета и весь путь отмены. Шаги 4–6 замыкают петлю
barge-in. Только после этого имеет смысл ASR: он самый заметный, но наименее рискованный
компонент — готовый бэкенд, готовая модель.

---

## 7. Стадии железа

| Stage | Вход | Выход | AEC | Что разрабатывается |
|-------|------|-------|-----|---------------------|
| 0 | гарнитура, push-to-talk | гарнитура | не нужен | контракт, TTS, отмена |
| 1 | гарнитура, открытый микрофон | гарнитура | не нужен (нет акустической связи) | **вся FSM barge-in, тайминги, ASR, KWS** |
| 2 | один USB-чип (гарнитура/адаптер) | тот же чип | софтовый (speexdsp/webrtc) | слияние в `audio_io`, опорный канал |
| 3 | ReSpeaker XVF3800 | усилитель + драйвер | аппаратный, опорный канал по USB | подмена ноды, DoA, пороги под зал |

Смысл Stage 1: гарнитура даёт акустическую развязку бесплатно, поэтому полный
full-duplex barge-in отлаживается **до** приезда XVF3800 и без единой строчки AEC.
Всё, что после этого меняется на Stage 3, — реализация `audio_frontend` и пороги.

Что нельзя делать на Stage 1: калибровать `enter_threshold`, `min_confidence` и
шумовой пол. Они завязаны на акустику зала и будут переснимать заново. Поэтому
все пороги — параметры в YAML, ни одной константы в коде.

Почему Stage 2 отдельная стадия, а не сразу 3: софтовый AEC на **одном** USB-чипе
работает (общий тактовый генератор), а на двух разных устройствах — нет, из-за
дрейфа частоты дискретизации фильтр расходится за десятки секунд. Stage 2 нужен
только чтобы отладить логику опорного канала и `filter_length_ms` до приезда железа;
если XVF3800 приедет раньше, чем понадобится, Stage 2 пропускается.

### Аудиоустройство: практика

PulseAudio держит USB-адаптер и не отдаёт `hw:`. `pasuspender` глушит всю машину.
Точечно:
```
pactl list short sinks | grep <card>
pactl suspend-sink <name> 1
pactl suspend-source <name> 1
```
На Orin в боевом образе PulseAudio не ставится вообще, ноды работают с `hw:` напрямую.
На ноутбуке — точечная приостановка только нужного устройства, остальная звуковая
подсистема жива.

---

## 8. Тесты и приёмочные метрики

**CI (без железа)**
- `test_chunker`: русские сокращения, инициалы, десятичные дроби, диапазоны, длинная клауза, пустой текст, текст без пунктуации.
- `test_scheduler`: вытеснение, очередь, `interruptible=false` против приоритета и против estop, переполнение очереди, `spoken_chars` при вытеснении.
- `test_sink_epoch`: инвариант «не более одного чанка после `bump()`»; проверяется на фейковом устройстве со счётчиком записанных блоков.
- `test_resampler`: длина выхода, отсутствие разрыва фазы на стыке чанков, сброс состояния.
- `test_turn_policy`: таблица (текст, тишина) → финализация.

**Железо (ручной прогон, лог в `SystemEvent`)**
- barge-in latency: p50 / p95 по 30 попыткам, требование p95 < 200 мс.
- false-wake rate под TTS: срабатываний в час при непрерывной речи робота.
- ASR: WER на фиксированном списке 30 фраз (экскурсионная лексика + команды).
- time-to-first-audio: от `send_goal` до первого сэмпла, требование < 400 мс.
- 30-минутный прогон: 0 xrun, 0 разрывов `first_sample`.

---

## 9. Открытые вопросы

1. **`epoch` как ns** — требует правки комментария в `CancelAll.msg` и согласования с mission/supervisor, которые тоже публикуют отмену.
2. **`gate_on_tts` в ASR** — на Stage 3 при работающем AEC ASR может слушать во время речи робота. Держать ли эту возможность или всегда гейтить по `SpeakingStatus`? Решается замером WER под TTS после приезда XVF3800.
3. **DoA-топик** — `Doa.msg` есть, издателя не будет до Stage 3. `audio_frontend` — правильное место, но у XVF3800 DoA идёт по I2C/USB control, а не в аудиопотоке; возможно, потребуется отдельная нода `xvf3800_control`.
4. **`AskUser.action`** — сервер живёт не здесь (это диалоговая семантика, mission/llm), но реализуется поверх `say` + `/asr/transcript`. Зафиксировать, что `guide_robot_voice` его **не** предоставляет.
