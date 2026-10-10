# guide_robot_voice

Аудио-I/O робота-экскурсовода: захват, VAD, wakeword/стоп-слово, ASR, TTS.
Пакет ничего не знает про тур, экспонаты или LLM — это чистый голосовой
слой с одной осмысленной семантикой поверх звука: приоритет, scope и epoch
отмены.README — практический справочник по факту
реализации (местами отличается от design, отличия отмечены отдельно).

`ament_python`, ROS 2 Humble. Голосовые ноды —
`rclpy.lifecycle.LifecycleNode`.

## Локальный wake-gated XVF-стенд — 7 октября 2026

Добавлен opt-in `wakeword_enabled:=true`. Прежние launch-команды без этого
флага сохраняют старый ASR-KWS/VAD admission. Production/DID автоматически
не переключается. Новая схема:

```text
XVF processed mono 16 kHz → локальный KWS → session manager → ASR → диалог
                  └─────→ VAD (endpointing, не разрешение фоновой речи)
                  └─────→ ASR ring 2,2 с (pre-roll 1,2 с + reserve 1 с)
```

Модель — неизменная `frames_06_confirmed` из пользовательской сессии
`live_20261007_110955_UTC_4e2839`: порог `0.8601541979408264`, три соседних
80-мс кадра, cooldown 2 с. SHA256 классификатора:
`66402d6fd6a90be9780c073d254bf48eecf27f8387ea67f36cd70578aeba22e1`.
Все три ONNX и manifest находятся в `models/firaya/`, включены в установку
пакета и проверяются по хешам. Повторное обучение/скачивание моделей при запуске
не выполняется. `scripts/package_wakeword_model.py` воспроизводит bundle из
frozen live-сессии; другой существующий bundle не перезаписывается.

KWS использует CPU, openWakeWord 0.6.0/ONNX и не отправляет аудио в сеть.
GigaAM тоже локальный: загружен и один раз прогрет при configure, затем
decode только после admission. Это НЕ означает, что весь робот offline:
LLM может обращаться к внешнему backend согласно своей конфигурации.

### Запуск на текущем компьютере

Сначала остановить прежний voice/audio launch через Ctrl+C в его терминале.
Нельзя одновременно запускать два владельца XVF или две копии voice-нод.
На хосте, из корня репозитория:

```bash
cd /home/sokovikov/Robo-guide
docker compose up -d voice-terminal
docker compose exec voice-terminal bash
```

`voice-terminal` — тот же x86 CPU образ, но без обязательного моторного UART.
Внутри контейнера, каждый новый терминал:

```bash
source /opt/ros/humble/setup.bash
cd /home/fabian/ros2_ws
```

Текущий компьютер уже имеет изолированные зависимости, использованные для
live-тестов. Для первого запуска можно использовать их без установки из сети:

```bash
export PYTHONPATH="/home/fabian/ros2_ws/src/datasets/wakeword/firaya_v1/experiments/_python_deps:${PYTHONPATH:-}"
python3 -c 'from importlib.metadata import version; print(version("openwakeword"))'
```

Ожидается `0.6.0`. Это временный путь зависимостей стенда, НЕ путь модели.
Для чистого образа/Jetson зависимости устанавливаются в сам образ: обновлён
`.docker/common/30-python-common.sh`. Либо в соответствующем контейнере:

```bash
python3 -m pip install 'numpy<2' scipy scikit-learn tqdm requests
python3 -m pip install --no-deps openwakeword==0.6.0
```

ONNX Runtime остаётся платформенным из образа; `--no-deps` не заменяет CUDA/CPU
пакет и не ставит TFLite. На Jetson нельзя копировать x86 `_python_deps`.
Установка в контейнер не сохраняется после его удаления/пересоздания; для
регулярной работы нужно пересобрать подходящий Docker-образ.

Собрать изменённые интерфейсы и потребителей (обычный install, без смены
существующего workspace на `--symlink-install`):

```bash
colcon build --packages-select \
  guide_robot_msgs guide_robot_voice guide_robot_face guide_robot_llm \
  guide_robot_mission_control guide_robot_bringup \
  --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source /home/fabian/ros2_ws/install/setup.bash
```

### Этап 1: только срабатывания имени, без ASR и LLM

После подготовки контейнера и сборки выше:

```bash
ros2 launch guide_robot_voice xvf3800_wakeword.launch.py
```

Здесь запущены только XVF audio owner, текущая frozen KWS и их lifecycle
manager. VAD, ASR, TTS, voice session manager и LLM **не запускаются**.
Каждое событие видно прямо в этом терминале:

```text
KWS candidate #1: ФИРАЯ score=0.901 end_sample=...
```

Это срабатывание детектора, не принятое обращение к роботу: `/speech/wakeword`
и команды диалога в этом тесте не публикуются. ASR-входы и legacy wake/cancel
выходы KWS изолированы в `/wakeword_test/...`. Сам микрофон остаётся общим,
поэтому прежние audio/voice/desk launch обязательно остановить.

Если нужен полный payload события, во втором терминале хоста:

```bash
cd /home/sokovikov/Robo-guide
docker compose exec voice-terminal bash
source /home/fabian/ros2_ws/install/setup.bash
ros2 topic echo /voice/wakeword_candidate
```

Простой протокол проверки (порог/веса не менять между тестами):

1. Подождать готовности обеих нод; затем 10 секунд тишины — ожидается 0 событий.
2. Пять раз «ФирАя» обычным голосом в 1 м, выдерживая 3–4 секунды между
   произнесениями — ожидается 5 событий. Cooldown модели 2 с: частые повторы
   нельзя считать независимыми попытками.
3. Минуту обычной речи без имени, включая «Феррари», «филлер», «Фонвизин»,
   «фидер», «факт», «Фарида», «фирма» — ожидается 0 событий.
4. «ФирАя, расскажи про эту картину»; через 4 секунды «Мы подошли к экспонату,
   ФирАя, объясни его историю»; через 4 секунды «Феррари — марка автомобиля»
   — ожидается 2 события, именно на имени.
5. Повторить 2–4 с расстояния 3 м, затем на фоне разговоров. Фоновые люди
   не должны произносить «Фирая» в отрицательной части теста.

Записать для каждого условия: сколько имён произнесено, сколько обнаружено,
сколько ложных событий было без имени. Совпадение общего количества само по
себе недостаточно: сверять моменты событий с произнесением. Нет beep/ответа
или реакции лица — это ожидаемо для изолированного теста.

Если сообщений нет даже на имени, проверить во втором терминале:

```bash
ros2 lifecycle get /xvf3800_audio_node
ros2 lifecycle get /wakeword_node
ros2 topic hz /audio/mic
ros2 topic echo /diagnostics
```

Обе ноды должны быть `active`, аудио должно идти; при 16-мс кадрах ожидается
примерно 62,5 сообщения/с. Отсутствие данных/ошибка configure — проблема
запуска, а не основание снижать порог KWS. Остановить тест через Ctrl+C.

### Этап 2: голосовой шлюз и ASR, ещё без LLM

Только аудиостек, без запуска LLM/миссий/моторов, ВМЕСТО trigger-only launch:

```bash
ros2 launch guide_robot_voice xvf3800_voice.launch.py \
  wakeword_enabled:=true
```

Новая модель слушает **`/audio/mic`**, не `/audio/mic_stereo`: флаг
`publish_stereo_debug:=true` ей не нужен. Если launch не проходит configure,
смотреть первую ошибку ноды; не включать legacy fallback ради запуска.

Для следующего этапа, когда проверен голосовой шлюз, полный настольный диалог
запускается ВМЕСТО предыдущей команды (остановить её через Ctrl+C):

```bash
ros2 launch guide_robot_bringup desk.launch.py \
  voice_profile:=xvf3800 wakeword_enabled:=true
```

Этот launch уже подключает face/session-state и исключает сырой VAD из
presence sources. Не запускает моторы, но LLM может использовать внешнюю сеть.
Аргумент проброшен также в `high_level_stack.launch.py`/`hardware.launch.py`;
запуск hardware и переключение production здесь не выполнялись.

### Ручная приёмка на микрофоне

На хосте открыть второй терминал: `docker compose exec voice-terminal bash`.
Внутри него: `source /home/fabian/ros2_ws/install/setup.bash`, затем
по одному наблюдаемому топику на терминал:

```bash
ros2 topic echo /voice/session_state
ros2 topic echo /speech/wakeword
ros2 topic echo /asr/transcript
ros2 topic echo /diagnostics
```

1. Без имени говорить обычные фразы/«Феррари», «фильтр», «Фарида» минуту.
   Ожидается `STATE_ARMED=8`, нет accepted wake/final; счётчик
   `voice/asr:utterances_total` не растёт. Если KWS всё-таки ошибся,
   фиксировать ложное wake — интеграция не улучшает качество классификатора.
2. «ФирАя, расскажи про эту картину» без паузы: accepted wake, admission,
   один содержательный final с `wakeword_admitted=true`, затем снова ARMED.
   Начало команды не должно обрезаться; `preroll_underflows` не растёт.
3. «ФирАя» отдельно: первый короткий wake-фрагмент (либо пустой ASR)
   не передаётся в диалог; остаётся окно 5 с для команды без повторного имени.
   Сказать «расскажи про картину», затем проверить один final и закрытие gate.
4. Сказать только имя и больше не говорить: окно закрывается через 5 с после
   обработки имени; последующая фоновая речь не допускается.
5. Повторить 1–4 с реальным шумом при тех же расстоянии/пороге/канале.

Без LLM лицо/ответ не появятся сами: отдельный аудиостенд публикует только
топики. Лицо в полном desk-стенде следует admission, а не фоновому `/vad`.

### Защиты и ограничения первого этапа

- `/voice/wakeword_candidate` — внутренний KWS-кандидат с capture session,
  end_sample и SHA модели. ТОЛЬКО session manager публикует принятое имя
  на `/speech/wakeword`. Stale/future/replayed/чужие события отклоняются.
- В ARMED нет даже `KWS_ONLY` сегментов GigaAM. ASR сохраняет PCM в ring,
  но partial/final jobs не создаются без ADMIT. Поздний REJECT fencing-ует
  результаты; queued rejected jobs не выполняют decode.
- Во время TTS KWS продолжает считать scores. Перебивание по имени включается
  отдельно через `wakeword_barge_in_enabled`; нужны свежие TTS И hardware
  heartbeats, interruptible output и фактический playback. Без подтверждённого
  AEC оно закрыто; для аппаратного эксперимента есть отдельный явный opt-in
  ниже. Непрерываемые реплики не обходятся. Legacy automatic VAD-barge-in
  остаётся выключенным, его не нужно включать для перебивания по имени.
- Самостоятельный «стоп» в ARMED сейчас НЕ детектируется: отдельной stop-KWS
  модели нет. После имени он доступен через ASR; это не замена emergency stop.
- После успешного полного ответа LLM открывается ограниченное follow-up окно
  для новой реплики без имени (описано ниже). Отдельная интеграция PROMPTED_LISTEN
  с mission FSM и grey-zone ASR verifier ещё не добавлены.
- Обновлён тип `Transcript`: все процессы нужно остановить и пересобрать,
  не смешивать старые и новые ROS message types в одном графе.
- Качество AEC/ASR в толпе и работа на Jetson требуют аппаратной приёмки.
  Режим остаётся стендовым кандидатом, не production-ready заявлением.

### Исправление окна после имени — 8 октября 2026

Живой лог показал: имя распознавалось как «ферая», «фрая», «сырая», «фрея»
либо пусто. Проверка точного списка отправляла эти варианты как обычный final
или discarded и немедленно закрывала admission. Расширять список имён для
устранения этой проблемы не требуется: KWS уже подтвердил акустическое имя.

В gated-профиле первая реплика теперь помечается `acoustic_wakeword` и
использует локальные CTC tokens/timestamps GigaAM. Пустой результат либо
короткий фрагмент у trigger не запускает LLM и оставляет окно команды.
Новая речь в этом окне получает отдельный ADMIT `command_after_name` и
pre-roll 160 мс вместо 1200 мс: старое имя не декодируется повторно.
Повторные KWS в открытом окне не заменяют его и не продлевают таймаут.
Старая VAD-тишина до onset не может финализировать новую реплику.

Это ограниченная эвристика wake-фрагмента, НЕ forced alignment и НЕ новый
лексический классификатор: короткая однословная команда, произнесённая до
позднего KWS без распознанного имени, может быть неоднозначна. Для такого
случая говорить команду после имени с паузой; однословная команда в отдельном
`command_after_name` сегменте не подавляется. Многословные фразы не обрезаются
по времени KWS, поскольку детектор может запаздывать. Без валидных таймингов
остаётся прежний точный fallback из четырёх вариантов, без fuzzy-расширений.
Веса/SHA/порог/confirmation/cooldown KWS неизменны.

После остановки desk через Ctrl+C в его терминале, в контейнере:

```bash
cd /home/fabian/ros2_ws
source /opt/ros/humble/setup.bash
source install/setup.bash
colcon build --packages-select guide_robot_voice
source install/setup.bash
export PYTHONPATH="/home/fabian/ros2_ws/src/datasets/wakeword/firaya_v1/experiments/_python_deps:${PYTHONPATH:-}"
ros2 launch guide_robot_bringup desk.launch.py \
  voice_profile:=xvf3800 wakeword_enabled:=true autostart:=true tts_backend:=silero
```

Перезапуск обязателен: работающие процессы не подхватывают Python-правки,
а временная настройка `ros2 param set ... wakeword_only_phrases` сбрасывается.
В логах ожидаются `wake fragment ...: жду команду`, затем
`Имя принято, окно команды: 5.0 с` и после новой речи
`Команда без повторного имени принята (command_after_name)`.

Повторная аппаратная проверка:

1. Пять раз «ФирАя» → пауза 1–2 с → «Расскажи интересный факт». После каждой
   команды дождаться полного окончания ответа. Ожидается по одному ответу.
2. «ФирАя» → пауза 1–2 с → «Привет». Однословная команда должна пройти.
3. Только «ФирАя» → 7 с тишины → фраза без имени. Ответа быть не должно.
4. «ФирАя, сколько будет два плюс два?» без паузы. Проверить, что прежний
   слитный сценарий сохранился, затем повторить с фоновыми разговорами.

CTC-эвристика проверена локально на старых WAV, но эти проверки не заменяют
новую живую приёмку окна и не доказывают качество распознавания в толпе.

Откат — остановить новый launch и запустить прежнюю команду с
`wakeword_enabled:=false` (либо без этого флага). Старые модели и raw dataset
не менялись. Новые deploy ONNX при коммите сохраняются через существующий Git LFS.

### Окно продолжения и перебивание по имени — 8 октября 2026

В gated desk-профиле теперь два разных окна:

- После отдельного «ФирАя» — прежние **5 секунд** для команды.
- После полностью произнесённого успешного ответа — **8 секунд** для следующей
  реплики без имени. Сначала ожидаются hardware idle/нулевой playback buffer и
  400 мс без output. Окно расходуется на одну реплику и снова открывается после
  её успешного ответа. После таймаута gate возвращается в ARMED.

Завершение ответа адресовано исходной ASR-реплике через `/dialog/turn_event`
(`device_session_id`, `utterance_id`, `start_sample`). Переход speaking=false
сам по себе не открывает окно: он бывает между фрагментами TTS и при отмене.
Неуспешный/прерванный или запоздалый ответ старого хода окно не открывает.
Нужен новый VAD onset после открытия, а не остаток голоса робота.

В этом окне допускаются и короткие ответы вроде «да». Но **идентификация
говорящего не добавлена**: чужой разговор в открытые 8 секунд тоже может пройти.
В толпе окно надо проверять отдельно; `followup_window_seconds:=0` выключает
продолжение без имени, меньший положительный интервал уменьшает экспозицию.

При включённом перебивании KWS «ФирАя» во время прерываемой озвучки отправляет
одну отмену ответа. ASR в этот момент не получает смесь имени и колонки.
После подтверждённой остановки playback и 400 мс echo hold открывается окно
команды на 5 секунд. **Сказать имя → дождаться остановки/индикации LISTENING →
сказать команду без имени.** Команда, слитно сказанная поверх ещё работающих
динамиков, этим вариантом не гарантируется. Простая речь без имени не отменяет
ответ; защищённые непрерываемые цели остаются защищёнными.

Перед обновлением **остановить весь прежний desk через Ctrl+C**: `Transcript`
теперь содержит capture metadata, добавлен `DialogTurnEvent`; нельзя смешивать
старые и новые процессы/типы в одном ROS-графе. Внутри `voice-terminal`:

```bash
cd /home/fabian/ros2_ws
source /opt/ros/humble/setup.bash
colcon build --packages-select \
  guide_robot_msgs guide_robot_voice guide_robot_face guide_robot_llm \
  guide_robot_mission_control guide_robot_bringup \
  --cmake-args -DAMENT_CMAKE_SYMLINK_INSTALL=OFF
source install/setup.bash
export PYTHONPATH="/home/fabian/ros2_ws/src/datasets/wakeword/firaya_v1/experiments/_python_deps:${PYTHONPATH:-}"
ros2 launch guide_robot_bringup desk.launch.py \
  voice_profile:=xvf3800 wakeword_enabled:=true \
  followup_window_seconds:=8.0 \
  wakeword_barge_in_enabled:=true \
  allow_unvalidated_wakeword_barge_in:=true \
  autostart:=true tts_backend:=silero
```

Это **тестовый opt-in без доказанного AEC**, не отметка `audio_profile_validated`:
робот может услышать собственное «ФирАя» из динамика и отменить себя. Обычный
launch без двух последних barge-in флагов оставляет перебивание выключенным,
но окно после ответа работает. При самоотменах вернуть
`wakeword_barge_in_enabled:=false` и разбираться с AEC до production.

Проверить последовательно:

1. «ФирАя, сколько будет два плюс два?» → дождаться ответа и строки
   `Окно без повторного имени открыто (followup_without_wakeword)` → через
   1–2 секунды «А три плюс три?». Ожидается второй ответ без имени.
2. Дождаться ответа и открытия окна → **10 секунд** молчания → «А пять плюс
   пять?». Ответа нет; с именем команда снова работает.
3. «ФирАя, расскажи длинную историю» → во время звучания сказать только
   «ФирАя». Звук остановлен, в логе один `barge-in принят`; после открытия
   `command_after_name` сказать «Сколько будет три плюс три?». Старый ответ
   не возобновляется, приходит новый.
4. Во время ответа говорить без имени — отмены нет. Затем проверить ответ,
   в котором робот сам произносит своё имя, без пользовательского обращения:
   самоотмены быть не должно. Это ключевой аппаратный AEC-тест.
5. Повторить в шумном месте; отдельно отметить чужую речь, принятую в follow-up
   окно, и ложные KWS во время озвучки. Весов/порога KWS между тестами не менять.

### Выполненные проверки

ROS-пакеты собраны в Humble x86 CPU образе без сети/аппаратных устройств.
Тесты покрывают VAD без admission, PCM framing, capture gaps/reconnect,
guarded TTS, queued-decode fencing, bounded name-only window и состояние лица.
После добавления follow-up/именованного barge-in: 266 тестов voice/face/admission
прошли (один copyright-тест пропущен), ещё 8 проверок полного mock-диалога
прошли отдельно. Среди них реальные ROS dialog/tool/Say с локальным MockLlmServer:
успешный/неуспешный ответ, отмена и сохранение capture origin при pending replay.
Реальные микрофон/динамики и внешний LLM этими проверками не запускались.
`scripts/check_wakeword_bundle.py` проверил все 30 пользовательских live-тестов:
все scores и моменты срабатывания совпали с frozen reference. Это проверка
переноса runtime, НЕ новое подтверждение accuracy/AEC.
Отчёт: `datasets/wakeword/firaya_v1/evaluation/integration_20261007/runtime_parity.json`.

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

В legacy-профилях владельцами устройств остаются `audio_frontend` и локальный
sink `tts_node`. В профиле XVF3800 единственный владелец обоих ALSA endpoint —
`xvf3800_audio_node`: старый `audio_frontend` не запускается, а `tts_node`
передаёт ему PCM через `RemoteSink`. Между VAD и ASR работает
`voice_session_manager`: он выдаёт `ADMIT/KWS_ONLY/REJECT` для конкретного
`device_session_id + utterance_id` и один владеет автоматическим barge-in.

## Ноды

### `audio_frontend`

Захват PCM (sounddevice/PortAudio), downmix в моно, DC-blocker (HPF 40 Гц),
gain, ресемплинг `device_rate → out_rate`, нарезка на кадры фиксированной
длины (`frame_ms`, по умолчанию 256 сэмплов @16кГц = 16мс). При xrun —
сброс состояния фильтров/ресемплера и `SystemEvent(severity=ERROR,
id="audio.xrun")`, без "латания нулями".

**Публикует**: `/audio/mic` (`AudioChunk`, BEST_EFFORT d20, 62.5 Гц),
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
`SpeakingStatus.interruptible=false` (stage4 §2.1/§2.2 — непрерываемая
связка контента, `guide_robot_semantic_map/lib/content_io.py` Chunk) тоже
глушит публикацию `CancelAll`, но только автоматический VAD-барж-ин: та же
staleness-проверка (400 мс), что и у `speaking`, отдельного окна для
`interruptible` нет. Стоп-слово (`wakeword_node`) и e-stop идут другим
путём (не через vad_node) и этим флагом не гейтятся.

В XVF-профиле `barge_in_enabled=false`: нода не принимает решение об отмене,
а публикует то же окно как sample-indexed `VadObservation`. Поле
`discontinuity` не позволяет FSM засчитать окна по разные стороны gap/reconnect
как непрерывное подтверждение речи.

**Публикует**: `/vad` (`VoiceActivity`, BEST_EFFORT d1, 31.25 Гц),
`/voice/vad_observation` (`VadObservation`, BEST_EFFORT d4),
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
буфер постоянно. В legacy-профиле фронт `/vad` сам открывает высказывание.
В XVF-профиле (`session_managed_input=true`) сегмент открывает только
`UtteranceControl`; pre-roll выбирается точно по `onset_sample`, а live PCM
добавляется с первого ещё не включённого sample index. Партиалы — троттлинг
до `partial_rate_hz`, декодируется
не весь буфер, а последние `partial_window_s` секунд. Финализация — по
`TurnPolicy` (`lib/turn_policy.py`): тишина `≥ base_silence_ms`, ИЛИ тишина
`≥ short_silence_ms` при синтаксически завершённом тексте, ИЛИ
`utterance_ms ≥ max_utterance_s` (страховка). Тишина берётся из
`state_duration` самого `/vad`, не считается заново.

До `ADMIT` KWS-кандидат публикуется только в `/voice/kws_partial`. Поздний
`REJECT` не позволяет уже запущенному worker опубликовать final. Внешние
`/asr/partial` и `/asr/transcript` получают только принятую реплику.

**Публикует**: `/asr/partial` (`Transcript`, `is_final=false`, BEST_EFFORT
d1, ~6 Гц), `/asr/transcript` (`Transcript`, `is_final=true`, RELIABLE d10),
`/voice/kws_partial`, `/voice/utterance_event`, `/diagnostics`.
`confidence=-1.0`, если бэкенд не отдаёт log-вероятности
(greedy_search у sherpa-onnx их не отдаёт — это не баг, это документированный
контракт `Transcript.msg`).

**Подписан на**: `/audio/mic`, `/vad`, `/voice/speaking` (для `gate_on_tts`),
`/voice/input_control`.

**Параметры**: `model_path`, `tokens_path`, `num_threads=2`,
`pre_roll_ms=300.0`, `partial_rate_hz=6.0`, `partial_window_s=5.0` (не из
design), `base_silence_ms=600.0`, `short_silence_ms=350.0`,
`max_utterance_s=20.0`, `min_final_chars=2`, `gate_on_tts=false`,
`session_managed_input=false` (`true` в XVF), `frame_id`. В XVF
`pre_roll_ms=500.0`.

### `voice_session_manager`

FSM очередности разговора. Получает соседние `VadObservation`, фактические
`SpeakingStatus`/`PlaybackState` и создаёт монотонный `utterance_id`. В тишине
сразу выдаёт `ADMIT`; во время TTS сначала `KWS_ONLY`, а после прохождения всех
guards — `ADMIT` и один `CancelAll(reason=barge_in)`. TTS затем выполняет уже
реализованный адресный `FencePlayback` для своего `stream_id/generation`.

Автоматический путь требует одновременно
`automatic_barge_in_enabled=true`, `audio_profile_validated=true`, свежих
статусов playback/speaking, `interruptible=true` и трёх соседних VAD-окон.
В текущем XVF-профиле первые два флага `false`, пока не завершён тест через
Supra и реальные динамики.

**Публикует**: `/voice/input_control`, `/voice/session_state`,
`/speech/cancel_all` (только подтверждённый automatic barge-in).

**Подписан на**: `/voice/vad_observation`, `/voice/speaking`,
`/audio/playback_state`, `/voice/utterance_event`.

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
`activation_phrases=["фирая", "фирайя", "фира я", "фи рая", "эй фирая", "слушай фирая"]`
(имя робота + типичные варианты его записи GigaAM — под TTS совпадение
только точное, поэтому варианты перечислены явно),
`stop_phrases=["стоп", "стой", "хватит", "замолчи"]`,
`fuzzy_max_distance=1`, `min_confidence=0.5`, `refractory_ms=1500.0`,
`frame_id`.

### `tts_node`

Silero TTS v5 (`v5_ru.pt`, спикер `xenia`) → `TextChunker` (клаузы) → `Scheduler`
(приоритет/scope/interruptible) → `Resampler` → выбранный sink. `local`
использует `EpochFencedSink` и PortAudio; `xvf3800` использует `RemoteSink`,
а ALSA открывает только `xvf3800_audio_node`. Единственный издатель
`/voice/speaking`. В XVF-профиле `speaking=true` основан на фактическом
`PlaybackState`, а не на начале синтеза. `/speech/cancel_all` — критический путь,
держится коротким (только `bump()` + `Scheduler.cancel()`); при
`reason=barge_in` latency (`now - msg.stamp`) считается и публикуется как
`SystemEvent` с heartbeat-таймера, не из колбэка отмены.

В XVF-профиле граница `spoken_text` тоже подтверждается аппаратным timeline:
клауза засчитывается только после того, как `presented_samples_estimate`
достиг числа принятых для неё сэмплов. Если в это время пришёл fence, частично
прозвучавшая клауза не попадает в `spoken_text`.

Полный компьютерный XVF-стенд запускается через
`xvf3800_voice.launch.py`. Он заменяет `audio_frontend` на
`xvf3800_audio_node` и поднимает TTS, VAD, session manager, wakeword и ASR.
Wakeword в этом launch переназначен на `/voice/kws_partial`. Автоматический
barge-in в этом профиле намеренно закрыт до измерительного подтверждения AEC.

Синтез клаузы, упавший с исключением (наблюдалось на реальном железе —
onnxruntime/GigaAM… не для TTS, но тот же класс проблем возможен и здесь)
повторяется один раз, если ничего ещё не ушло в сток; иначе — `STATUS_FAILED`
с корректной уборкой планировщика, а не зависание.

**Действие**: `say` (`Say.action`) — `text`, `voice`, `priority`, `scope`,
`interruptible`, `max_duration` → `status`, `spoken_text`, `spoken_chars`,
`spoken_duration`, `message`; feedback — `clause_index`, `clause_count`,
`progress`, `current_clause`.

**Публикует**: `/voice/speaking` (`SpeakingStatus`, RELIABLE+TRANSIENT_LOCAL
d1, 5 Гц heartbeat + по изменению — включая `interruptible` активного
высказывания, stage4 §2.1), `/diagnostics`, `/system_event` (barge-in
latency).

**Подписан на**: `/speech/cancel_all`.

**Параметры**: `backend="silero"` (`silero`|`piper`|`null` — `null` синтезирует тон,
режим измерений без модели), `model_path`, `speaker=xenia`, `config_path`, `speaker_id=0`
(для piper), `length_scale=1.0`, `silero_rate="100%"` (SSML `<prosody rate>`,
проценты или `fast`/`x-fast`), `sentence_pause_ms=0` (явный `<break>` между
предложениями вместо модельной паузы ~400 мс), `trailing_silence_ms=-1`
(обрезка тишины в конце клаузы, -1 -- не трогать), `device`, `device_rate=0` (0 → частота бэкенда),
`block_ms=20`, `periods=3`, `channels=2`, `allow_shared=false`,
`max_queue_ms=600`, `fade_out_ms=80`, `keepalive_dbfs=0.0` (<0 → тон
`keepalive_hz=20.0` вместо нулей в паузах, против авто-mute USB-кодека;
см. «Известные грабли»), `min_chars=40`, `max_clause_chars=180`,
`chars_per_second=14.0`, `heartbeat_hz=5.0`, `max_queue=8`,
`warmup_text="Система готова"`, `default_priority=50` (подставляется,
если `Say.Goal.priority == 0`).

## Контракт сообщений (сводно)

| Топик | Тип | QoS | Издатель | Подписчики |
|---|---|---|---|---|
| `/audio/mic` | `AudioChunk` | BEST_EFFORT d20 | audio_frontend | vad_node, asr_node |
| `/audio/mic_raw` | `AudioChunk` | BEST_EFFORT d20 | audio_frontend (опц.) | диагностика |
| `/vad` | `VoiceActivity` | BEST_EFFORT d1 | vad_node | asr_node |
| `/voice/vad_observation` | `VadObservation` | BEST_EFFORT d4 | vad_node | voice_session_manager |
| `/voice/input_control` | `UtteranceControl` | RELIABLE d10 | voice_session_manager | asr_node |
| `/voice/utterance_event` | `UtteranceEvent` | RELIABLE d10 | asr_node | voice_session_manager |
| `/voice/session_state` | `VoiceSessionState` | RELIABLE, TRANSIENT_LOCAL d1 | voice_session_manager | диагностика |
| `/voice/kws_partial` | `Transcript` | BEST_EFFORT d1 | asr_node | wakeword_node (XVF) |
| `/speech/wakeword` | `Wakeword` | RELIABLE d1 | wakeword_node | — |
| `/asr/partial` | `Transcript` | BEST_EFFORT d1 | asr_node | wakeword_node |
| `/asr/transcript` | `Transcript` | RELIABLE d10 | asr_node | — |
| `/voice/speaking` | `SpeakingStatus` | RELIABLE, TRANSIENT_LOCAL d1 | tts_node | vad_node, asr_node, wakeword_node |
| `/speech/cancel_all` | `CancelAll` | RELIABLE d1 | vad_node (legacy), voice_session_manager (XVF), wakeword_node | tts_node |
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
| `v5_ru.pt` | tts_node | Silero TTS v5 (`xenia`) |
| `ru_RU-irina-medium.onnx(.json)` | tts_node (backend=piper) | Piper voices (HuggingFace) |
| `gigaam_v3_ctc_int8.onnx` + `gigaam_v3_ctc_tokens.txt` | asr_node | `Smirnov75/GigaAM-v3-sherpa-onnx` (HuggingFace) |

Пути в `config/voice.yaml` — через `$(find-pkg-share guide_robot_voice)`,
поэтому запуск через `voice.launch.py`/`tts_only.launch.py` обязан
оборачивать `parameters` в `launch_ros.parameter_descriptions.ParameterFile(
..., allow_substs=True)` — без этого флага подстановка не разворачивается
и путь к модели просто не существует ни на одной машине.

## Python-зависимости вне package.xml

`piper-tts`, `sherpa-onnx`, `soxr` — пакеты моделей/DSP, ставятся только
через pip, рosdep-ключа нет. `scipy` — мягкая зависимость (polyphase-ресемплинг
и DC-blocker; без неё модули импортируются и работают, но с более грубым
линейным ресемплингом/наивным Python-циклом). `soxr` (`lib/resampler.py`)
— стейтфул-полифазный `soxr.ResampleStream`, единственный путь, который
корректно держит состояние фильтра НА ГРАНИЦЕ блоков (`resample_poly` этого
не делает: независимая фильтрация каждого блока даёт ~19dB потерь SNR на
стыках, даже если длина блоков посчитана точно); при отсутствии `soxr`
падение на `scipy`/линейную интерполяцию сохраняется как и раньше.
`sounddevice` уже объявлен в `package.xml` (`python3-sounddevice`).
`.docker/common/30-python-common.sh`/`40-ml-*.sh` ставят все четыре
(`piper-tts`/`sherpa-onnx`/`scipy`/`soxr`) для x86-cpu/x86-gpu/jetson;
`40-ml-jetson-cpu.sh` — минимальный вариант без голосового стека вовсе,
их там нет намеренно.

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

- **После reboot `audio_frontend` мерял ~49454 Гц вместо 48000 (~3%)**
  и валил activate -- supervisor не поднимал voice/mission. USB/Pulse
  на холодном старте плюс `sleep(2)` короче окна. Теперь это warning,
  отказ только если нет кадров или сплошные нули.
- **`device: ""` на двух нодах разом = общий ALSA `default`.** Базовый
  `config/voice.yaml` не задаёт `device` ни для `audio_frontend`, ни для
  `tts_node` (пусто = системное умолчание). На реальном железе с двумя
  раздельными устройствами (микрофон и колонки) это означает, что оба
  процесса откроют один и тот же `default` одновременно -- вход и выход
  одной ALSA-цепочки. Наблюдалось как шторм `snd_pcm_recover: underrun
  occurred` и разогрев Piper за 6+ секунд вместо ~150 мс. Лечится
  `voice_headset.yaml` (или любым оверлеем с явными разными `device`
  для входа и выхода).
- **USB-кодек Generic AB13X (0020:0b21, на роботе с 14.09.2026) глушит
  выход на цифровой тишине.** После ~1 с ровных нулей (800 мс ещё
  терпит) он отключает выход и «просыпается» 250-400 мс: столько речи
  пропадает в начале каждой реплики и после любой паузы длиннее
  секунды. На monitor-выходе Pulse звук целый, теряется уже в железе --
  одинаково через `pulse` и через `hw:2,0`. Тихий, но ненулевой сигнал
  (-50 dBFS) его не усыпляет; порог детектора между -66 и -72 dBFS,
  ±4 LSB дизера мало. Лечится `keepalive_dbfs: -60.0` (20 Гц в паузах,
  `lib/sink.KeepAliveTone`) -- измерено 15.09.2026 микрофоном Fifine
  при воспроизведении тональных пачек через паузы 0.3-6 с.
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
