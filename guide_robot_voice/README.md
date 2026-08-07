# guide_robot_voice

Аудио-I/O робота-экскурсовода: захват, VAD, wakeword/стоп-слово, ASR, TTS.
Пакет ничего не знает про тур, экспонаты или LLM — это чистый голосовой
слой с одной осмысленной семантикой поверх звука: приоритет, scope и epoch
отмены.README — практический справочник по факту
реализации (местами отличается от design, отличия отмечены отдельно).

`ament_python`, ROS 2 Humble. Все пять нод — `rclpy.lifecycle.LifecycleNode`.

## Топология

```
                    ┌──────────────┐
   микрофон ──────► │audio_frontend│──/audio/mic───┬───────────────┬──────────────┐
                    └──────────────┘               │               │              │
                                                   ▼               ▼              │
                                              ┌──────────┐   ┌───────────┐        │
                                              │ vad_node │   │ (pre-roll)│        │
                                              └────┬─────┘   └─────┬─────┘        │
                                                   │/vad           │              │
                                 ┌─────────────────┼───────────────┘              │
                                 ▼                 ▼                              │
                          /speech/cancel_all  ┌──────────┐                        │
                            (barge-in)        │ asr_node │◄───────────────────────┘
                                 │            └────┬─────┘
                                 │     /asr/partial│ /asr/transcript
                                 │                 ▼
                                 │         ┌───────────────┐
                                 │         │ wakeword_node │──/speech/cancel_all
                                 │         └───────────────┘   (стоп-слово)
                                 ▼
                            ┌──────────┐
                            │ tts_node │◄── action "say"
                            └────┬─────┘
                                  │/voice/speaking (динамик)
                                  ▼
                            /diagnostics, /system_event -- пишут все ноды
```

Владелец устройства захвата — только `audio_frontend`; владелец устройства
воспроизведения — только `tts_node`. Никто другой PCM напрямую не открывает.

## Ноды

### `audio_frontend`

Захват PCM (sounddevice/PortAudio), downmix в моно, DC-blocker (HPF 40 Гц),
gain, ресемплинг `device_rate → out_rate`, нарезка на кадры фиксированной
длины (`frame_ms`, по умолчанию 256 сэмплов @16кГц = 16мс). При xrun —
сброс состояния фильтров/ресемплера и `SystemEvent(severity=ERROR,
id="audio.xrun")`, без "латания нулями".

**Публикует**: `/audio/mic` (`AudioChunk`, BEST_EFFORT d5, 62.5 Гц),
`/audio/mic_raw` (то же, `device_rate`, только если `publish_raw:=true`,
диагностика), `/diagnostics` (1 Гц), `/system_event` (по xrun).

**Параметры** (`device`, `device_rate=48000`, `out_rate=16000`,
`frame_ms=16`, `channels_in=1`, `periods=3`, `gain_db=0.0`, `hpf_hz=40.0`,
`publish_raw=false`, `frame_id="mic_array"`, `aec.enabled=false`,
`aec.backend="none"`, `aec.filter_length_ms=200.0`) — `aec.*` объявлены,
но не реализованы (Stage 2+).

### `vad_node`

Silero VAD v5 (ONNX) поверх окон 512 сэмплов (2 кадра `/audio/mic`),
гистерезис `enter_threshold`/`exit_threshold`/`enter_windows`/`hangover_ms`.
Barge-in: независимый от базового гистерезиса счётчик `barge_in_min_windows`
на сырой вероятности — при срабатывании и живом `/voice/speaking` публикует
`CancelAll(scope=SCOPE_ALL, reason=REASON_BARGE_IN, stamp=<момент начала
речи>, epoch=<now().nanoseconds>)`. `require_aec_for_barge_in=true` глушит
barge-in целиком (AEC ещё не существует — нет подтверждения, нет и barge-in).

**Публикует**: `/vad` (`VoiceActivity`, BEST_EFFORT d1, 31.25 Гц),
`/speech/cancel_all` (`CancelAll`, только при barge-in), `/diagnostics`.

**Подписан на**: `/audio/mic`, `/voice/speaking` (для гейта barge-in,
протухание >400мс → тихо `speaking=false`).

**Параметры**: `model_path`, `enter_threshold=0.65`, `exit_threshold=0.35`,
`enter_windows=2`, `hangover_ms=400.0`, `min_speech_ms=120.0` (короткие
сегменты помечаются постфактум, не задерживают публикацию), `frame_id`,
`barge_in_enabled=true`, `barge_in_min_windows=2`,
`require_aec_for_barge_in=false`.

### `asr_node`

GigaAM v3 CTC через `sherpa_onnx.OfflineRecognizer` (**не** `OnlineRecognizer`
— см. «Отличия от design» ниже). Копит `/audio/mic` в pre-roll кольцевой
буфер постоянно; по фронту `/vad` открывает высказывание со снимком
pre-roll внутри. Партиалы — троттлинг до `partial_rate_hz`, декодируется
не весь буфер, а последние `partial_window_s` секунд. Финализация — по
`TurnPolicy` (`lib/turn_policy.py`): тишина `≥ base_silence_ms`, ИЛИ тишина
`≥ short_silence_ms` при синтаксически завершённом тексте, ИЛИ
`utterance_ms ≥ max_utterance_s` (страховка). Тишина берётся из
`state_duration` самого `/vad`, не считается заново.

**Публикует**: `/asr/partial` (`Transcript`, `is_final=false`, BEST_EFFORT
d1, ~6 Гц), `/asr/transcript` (`Transcript`, `is_final=true`, RELIABLE d10),
`/diagnostics`. `confidence=-1.0`, если бэкенд не отдаёт log-вероятности
(greedy_search у sherpa-onnx их не отдаёт — это не баг, это документированный
контракт `Transcript.msg`).

**Подписан на**: `/audio/mic`, `/vad`, `/voice/speaking` (для `gate_on_tts`).

**Параметры**: `model_path`, `tokens_path`, `num_threads=2`,
`pre_roll_ms=300.0`, `partial_rate_hz=6.0`, `partial_window_s=5.0` (не из
design), `base_silence_ms=600.0`, `short_silence_ms=350.0`,
`max_utterance_s=20.0`, `min_final_chars=2`, `gate_on_tts=false`, `frame_id`.

### `wakeword_node`

`backend=asr_kws` (Stage 1, реализован): подписка на `/asr/partial`,
нечёткий поиск по Левенштейну (`lib/keyword_spotter.py`) отдельно для
`activation_phrases` и `stop_phrases`, рефрактерный гейт per-phrase
(`refractory_ms`). Стоп-фраза публикует `CancelAll` сама (L1, не ждёт
mission). `backend=oww` объявлен, но при выборе бросает `NotImplementedError`
(Stage 3 — своя модель на синтетике Piper, ещё не обучена).

**Публикует**: `/speech/wakeword` (`Wakeword`, RELIABLE d1),
`/speech/cancel_all` (только по стоп-фразе), `/diagnostics`.

**Подписан на**: `/asr/partial`, `/voice/speaking` (для `tts_active` —
протухание логирует явный `WARN`, в отличие от других нод: без честного
`tts_active` метрика false-wake-under-TTS считается неверно).

**Параметры**: `backend="asr_kws"`,
`activation_phrases=["робот", "слушай робот"]`,
`stop_phrases=["стоп", "стой", "хватит", "замолчи"]`,
`fuzzy_max_distance=1`, `min_confidence=0.5`, `refractory_ms=1500.0`,
`frame_id`.

### `tts_node`

Piper (`ru_RU-irina-medium`) → `TextChunker` (клаузы) → `Scheduler`
(приоритет/scope/interruptible) → `Resampler` → `EpochFencedSink`
(callback-режим PortAudio, epoch-fencing на отмене) → ALSA. Единственный
издатель `/voice/speaking`. `/speech/cancel_all` — критический путь,
держится коротким (только `bump()` + `Scheduler.cancel()`); при
`reason=barge_in` latency (`now - msg.stamp`) считается и публикуется как
`SystemEvent` с heartbeat-таймера, не из колбэка отмены.

Синтез клаузы, упавший с исключением (наблюдалось на реальном железе —
onnxruntime/GigaAM… не для TTS, но тот же класс проблем возможен и здесь)
повторяется один раз, если ничего ещё не ушло в сток; иначе — `STATUS_FAILED`
с корректной уборкой планировщика, а не зависание.

**Действие**: `say` (`Say.action`) — `text`, `voice`, `priority`, `scope`,
`interruptible`, `max_duration` → `status`, `spoken_text`, `spoken_chars`,
`spoken_duration`, `message`; feedback — `clause_index`, `clause_count`,
`progress`, `current_clause`.

**Публикует**: `/voice/speaking` (`SpeakingStatus`, RELIABLE+TRANSIENT_LOCAL
d1, 5 Гц heartbeat + по изменению), `/diagnostics`, `/system_event`
(barge-in latency).

**Подписан на**: `/speech/cancel_all`.

**Параметры**: `backend="piper"` (`piper`|`null` — `null` синтезирует тон,
режим измерений без модели), `model_path`, `config_path`, `speaker_id=0`,
`length_scale=1.0`, `device`, `device_rate=0` (0 → частота бэкенда),
`block_ms=20`, `periods=3`, `channels=2`, `allow_shared=false`,
`max_queue_ms=600`, `min_chars=40`, `max_clause_chars=180`,
`chars_per_second=14.0`, `heartbeat_hz=5.0`, `max_queue=8`,
`warmup_text="Система готова"`, `default_priority=50` (подставляется,
если `Say.Goal.priority == 0`).

## Контракт сообщений (сводно)

| Топик | Тип | QoS | Издатель | Подписчики |
|---|---|---|---|---|
| `/audio/mic` | `AudioChunk` | BEST_EFFORT d5 | audio_frontend | vad_node, asr_node |
| `/audio/mic_raw` | `AudioChunk` | BEST_EFFORT d5 | audio_frontend (опц.) | диагностика |
| `/vad` | `VoiceActivity` | BEST_EFFORT d1 | vad_node | asr_node |
| `/speech/wakeword` | `Wakeword` | RELIABLE d1 | wakeword_node | — |
| `/asr/partial` | `Transcript` | BEST_EFFORT d1 | asr_node | wakeword_node |
| `/asr/transcript` | `Transcript` | RELIABLE d10 | asr_node | — |
| `/voice/speaking` | `SpeakingStatus` | RELIABLE, TRANSIENT_LOCAL d1 | tts_node | vad_node, asr_node, wakeword_node |
| `/speech/cancel_all` | `CancelAll` | RELIABLE d1 | vad_node, wakeword_node | tts_node |
| `/diagnostics` | `DiagnosticArray` | стандартный | все ноды | — |
| `/system_event` | `SystemEvent` | RELIABLE d10 | audio_frontend, tts_node | — |
| `say` (action) | `Say` | — | — (сервер: tts_node) | — |

QoS-профили собраны в `guide_robot_voice/lib/qos.py` одним местом —
единственный модуль в `lib/`, которому разрешено импортировать `rclpy`.

`epoch` в `CancelAll` — `self.get_clock().now().nanoseconds`, а не счётчик
на публикующую ноду (design §0.1: со счётчиком на ноду несколько
издателей `CancelAll` гонятся за независимыми последовательностями и
получатель может отбросить легитимную отмену).

## Модели (git-lfs, `models/`)

| Файл | Кто использует | Источник |
|---|---|---|
| `silero_vad.onnx` | vad_node | `snakers4/silero-vad` |
| `ru_RU-irina-medium.onnx(.json)` | tts_node | Piper voices (HuggingFace) |
| `gigaam_v3_ctc_int8.onnx` + `gigaam_v3_ctc_tokens.txt` | asr_node | `Smirnov75/GigaAM-v3-sherpa-onnx` (HuggingFace) |

Пути в `config/voice.yaml` — через `$(find-pkg-share guide_robot_voice)`,
поэтому запуск через `voice.launch.py`/`tts_only.launch.py` обязан
оборачивать `parameters` в `launch_ros.parameter_descriptions.ParameterFile(
..., allow_substs=True)` — без этого флага подстановка не разворачивается
и путь к модели просто не существует ни на одной машине.

## Python-зависимости вне package.xml

`piper-tts`, `sherpa-onnx` — пакеты моделей, ставятся только через pip,
рosdep-ключа нет. `scipy` — мягкая зависимость (polyphase-ресемплинг и
DC-blocker; без неё модули импортируются и работают, но с более грубым
линейным ресемплингом/наивным Python-циклом). `sounddevice` уже объявлен
в `package.xml` (`python3-sounddevice`). На момент написания в
`.docker/Dockerfile*` присутствуют `sounddevice`/`scipy`, но **не**
`piper-tts`/`sherpa-onnx` — решение добавлять их в образ за вами.

## Запуск

```bash
# Весь стек, ноды unconfigured -- подъём вручную или через mission
ros2 launch guide_robot_voice voice.launch.py

# Весь стек, автоподъём в порядке tts_node -> audio_frontend -> vad_node
# -> wakeword_node -> asr_node (design §3, критично при autostart:=true)
ros2 launch guide_robot_voice voice.launch.py autostart:=true

# Отладка одного TTS без микрофона/VAD/ASR
ros2 launch guide_robot_voice tts_only.launch.py

# Оверлей для разработки на гарнитуре -- ОБЯЗАТЕЛЕН на реальном железе,
# иначе capture и playback оба уходят на ALSA "default" и соревнуются за
# устройство (см. "Известные грабли" ниже)
ros2 launch guide_robot_voice voice.launch.py autostart:=true \
    params_file:=$(ros2 pkg prefix guide_robot_voice)/share/guide_robot_voice/config/voice_headset.yaml
```

Ручной подъём (если `autostart:=false`):

```bash
ros2 lifecycle set /tts_node configure
ros2 lifecycle set /tts_node activate
ros2 lifecycle set /audio_frontend configure
ros2 lifecycle set /audio_frontend activate
ros2 lifecycle set /vad_node configure
ros2 lifecycle set /vad_node activate
ros2 lifecycle set /wakeword_node configure
ros2 lifecycle set /wakeword_node activate
ros2 lifecycle set /asr_node configure
ros2 lifecycle set /asr_node activate
```

Через супервизор (`guide_robot_supervisor`, группа `voice`,
`config/supervisor.yaml`/`config/supervisor_slam.yaml`, `optional: true`):
`lifecycle_manager_voice` в `voice.launch.py` теперь запускается
безусловно (не под `IfCondition(autostart)`), `autostart` пробрасывается
в него как обычный параметр (default `false`) — сервис `~/manage_nodes`
обязан существовать для супервизора вне зависимости от того, кто
инициирует `STARTUP`. На практике весь стек (voice + semantic_map +
mission_control) поднимается сразу через
`guide_robot_bringup/launch/high_level_stack.launch.py` — см.
`guide_robot_mission_control/README.md`.

## Известные грабли

- **`device: ""` на двух нодах разом = общий ALSA `default`.** Базовый
  `config/voice.yaml` не задаёт `device` ни для `audio_frontend`, ни для
  `tts_node` (пусто = системное умолчание). На реальном железе с двумя
  раздельными устройствами (микрофон и колонки) это означает, что оба
  процесса откроют один и тот же `default` одновременно -- вход и выход
  одной ALSA-цепочки. Наблюдалось как шторм `snd_pcm_recover: underrun
  occurred` и разогрев Piper за 6+ секунд вместо ~150 мс. Лечится
  `voice_headset.yaml` (или любым оверлеем с явными разными `device`
  для входа и выхода).
- **PulseAudio держит `hw:` устройство.** См. §7 design-документа --
  `pactl suspend-sink`/`suspend-source` точечно, не `pasuspender` на всю
  машину.
- **`nav2_lifecycle_manager` и `bond`.** Наши ноды -- обычные
  `rclpy.lifecycle.LifecycleNode`, bond не создают. В обоих launch-файлах
  `bond_timeout: 0.0` выставлен явно -- без этого менеджер валит bringup
  через `bond_timeout` секунд, даже если ноды сами активировались успешно.
- **`colcon test` может не находить pytest-тесты**, если в окружении
  собственный анализ плагинов pytest ломается на несовместимой версии
  `anyio`/`pytest`. Обходится: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m
  pytest test/` из каталога пакета -- к самому пакету отношения не имеет.

## Тесты

```bash
cd guide_robot_voice
python3 -m pytest test/ -q
```

Юнит-тесты (`lib/`) не требуют ни ROS, ни моделей, ни звуковой карты --
CI-safe по построению. Ноды (`audio_frontend.py`, `vad_node.py`, ...)
юнитами не покрыты сознательно -- design разносит верификацию по шагам
(`§6`) на реальном железе; см. `guide_robot_voice_design.md`.

## Отличия от `guide_robot_voice_design.md`

Кроме восьми пунктов из design §0, при реализации накопились ещё:

1. **ASR не потоковый.** Design закладывал `sherpa_onnx.OnlineRecognizer`.
   Единственный публичный экспорт GigaAM v3 CTC для sherpa-onnx не несёт
   метаданных cache-aware streaming (падает на `Init` с "window_size does
   not exist in the metadata") -- честного стриминга для GigaAM в
   публичных сборках просто нет. `asr_node` гоняет `OfflineRecognizer`
   повторно на скользящем окне (`partial_window_s`, новый параметр) для
   партиалов и один раз на всём высказывании для финала. См.
   `lib/asr_model.py`.
2. **`lib/` шире дерева файлов из design §5.** Добавлены (не были
   запланированы явно, но следуют тому же принципу "чистая логика без
   rclpy отдельно от ROS-обвязки"): `backends.py` (TTS-бэкенды,
   `NullBackend` для измерений без модели), `audio_device.py` (резолв
   устройства по имени), `qos.py` (все QoS-профили пакета), `dc_blocker.py`,
   `vad_hysteresis.py`, `vad_model.py`, `asr_model.py`, `keyword_spotter.py`.
   `ring.py` реализован полнее, чем требовалось на момент появления
   (сразу с `max_samples`-вытеснением и `snapshot()`, которые понадобились
   только в asr_node).
3. **`Scheduler.cancel()` принимает `reason`.** В design `interruptible`
   защищает от отмены "кроме `scope=SAFETY`"; на практике нужен ещё и
   `reason=REASON_ESTOP` независимо от scope -- добавлен параметр, гейт
   применяется одинаково и к активной цели, и к очереди.
4. **`voice_headset.yaml` поправлен под реальную гарнитуру.** Исходный
   оверлей указывал `tts_node.device: "hw:2,0"` -- это устройство
   capture-only (0 каналов вывода) на используемом железе. TTS выведен на
   `hw:1,0` (аналоговый выход).
