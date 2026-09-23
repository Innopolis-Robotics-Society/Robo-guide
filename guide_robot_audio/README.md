# guide_robot_audio

Пакет содержит единственного владельца ALSA capture/playback ReSpeaker XVF3800.
Он не знает о диалоге, экскурсии или TTS-тексте: его граница ответственности —
USB-устройство, PCM и непрерывность сэмплов.

## Реализовано

- проверка USB serial конкретной платы через sysfs;
- прямое открытие capture и playback `hw:` без скрытого ресемплинга;
- проверка фактической частоты и числа каналов;
- извлечение настраиваемого processed-канала из stereo capture;
- публикация mono `AudioChunk` в `/audio/mic` с `device_session_id` и индексом
  `first_sample`;
- опциональная диагностическая публикация обоих USB capture-каналов в
  `/audio/mic_stereo` (`publish_stereo_debug=true`);
- ограниченная очередь mono PCM с дублированием в оба playback-канала;
- `BeginPlayback`, `PlayPcm` и `FencePlayback` с защитой по
  `device_session_id`/`stream_id`/`generation`;
- фактический `/audio/playback_state` по прогрессу ALSA;
- короткий fade-out и очистка старого PCM без остановки capture;
- lifecycle configure/activate/deactivate/cleanup.

`tts_node` подключается к этим интерфейсам через `RemoteSink` и больше не
открывает XVF3800 через PortAudio.

## Стендовый запуск

```bash
colcon build --symlink-install --packages-up-to guide_robot_audio
source install/setup.bash
ros2 launch guide_robot_audio xvf3800_audio.launch.py
```

В другом терминале:

```bash
source install/setup.bash
ros2 topic hz /audio/mic
ros2 topic echo /audio/mic --once --field first_sample
```

Ожидаемая частота `/audio/mic` — около 62,5 Гц: один кадр каждые 16 мс.

Совместный стенд audio owner + TTS:

```bash
ros2 launch guide_robot_voice xvf3800_tts.launch.py tts_backend:=null
```

`null` воспроизводит тестовый тон. Для настоящего Silero TTS параметр можно
не указывать. На стенде подтверждены непрерывные 62,5 Гц `/audio/mic`,
`PLAYING → IDLE`, `FENCED` после отмены и новая реплика после fencing.

Полный стенд с VAD/ASR без legacy `audio_frontend`:

```bash
ros2 launch guide_robot_voice xvf3800_voice.launch.py
```

Для сравнения обоих UAC capture-каналов диагностический топик включается
отдельно:

```bash
ros2 launch guide_robot_voice xvf3800_voice.launch.py publish_stereo_debug:=true
python3 scripts/xvf3800_ros_channel_probe.py --seconds 10 --record /tmp/xvf.wav
```

### Предварительное сравнение capture-каналов

Компьютерный full-duplex стенд 21 сентября 2026 года, 16 кГц, без разрывов
`first_sample` и без клиппинга в трёх контролируемых интервалах:

| Режим | Channel 0 RMS | Channel 1 RMS |
|---|---:|---:|
| playback-only, человек молчит | −40,2 dBFS | −32,3 dBFS |
| human-only, TTS молчит | −25,6 dBFS | −24,3 dBFS |
| double-talk | −28,1 dBFS | −21,7 dBFS |

Channel 0 сохранил человеческую речь почти на уровне channel 1, но остаток
собственного playback был примерно на 8 dB ниже. Поэтому для стенда выбран
`processed_channel: 0`. Это предварительное решение: испытание обязательно
повторяется через Supra и динамики в окончательной акустике корпуса.

## Пока не реализовано

- отдельная публикация счётчиков ALSA XRUN в `/diagnostics`;
- переподключение после извлечения USB;
- чтение DOA/control API;
- окончательный выбор processed-канала по испытанию AEC с колонкой.
