# Хостовый сервис управления Robo-guide

`guide_launcher` — самостоятельный сервис без зависимости от ROS Python.
Он обслуживает API v1 на `127.0.0.1:8089`, PIN/RFID-сессии, запуск ROS-стека,
наблюдение состояния и операции. ROS-мост `guide_robot_api` находится на loopback:8091;
один внутренний WebSocket обслуживает всех внешних клиентов.

[Полный контракт, примеры, семантика операций и миграция](guide_launcher/spec/README.md).
Локальная документация: `http://127.0.0.1:8089/api/v1/docs/index.html`.

## Установка на Jetson

```bash
scripts/jetson-launcher/install.sh --preflight-only
DRY_RUN=1 scripts/jetson-launcher/install.sh
sudo scripts/jetson-launcher/install.sh
```

Установщик проверяет Docker, mount workspace, host networking, DDS-окружение,
права пользователя и kiosk. Устанавливает host Python-модули и необязательный
`reference_ui` в `/opt/guide-launcher`, unit `guide-launcher.service`, конфиг
`/etc/guide-launcher/config.yaml`. Python-зависимости: aiohttp, PyYAML, pyserial.
Компоненты ROS необходимо собрать отдельно. Настройки по умолчанию — в config.example.yaml.

Существующий config сохраняется. При миграции вручную обновить `promo_dir`, `web_dir`,
добавить `serve_ui: true` для штатной панели и `public_tours: [expo_one]` для посетителей.
`serve_ui: false` отключает панель; отсутствие её файлов не мешает API.
В `web_dir` можно разместить собственную сборку на том же origin.

Параметры среды установщика: CONTAINER, SERVICE_USER, CONTAINER_USER, ETC_DIR,
BIN_DIR, OPT_DIR, UNIT_DIR, HOST_PYTHON. В обычной установке достаточно дефолтов.
Общий bridge token создаётся в `.guide_launcher/bridge_token` и не передаётся браузерам.
RFID: [прошивка и настройка](../../firmware/operator_rfid/RFID_README.md).

## Запуск и диагностика

```bash
PYTHONPATH=scripts/jetson-launcher python3 -m guide_launcher --config /etc/guide-launcher/config.yaml
curl http://127.0.0.1:8089/api/v1/health
curl http://127.0.0.1:8089/api/v1/state
systemctl status guide-launcher
journalctl -u guide-launcher
```

Health не зависит от ROS и не вызывает ListTours. Старт/рестарт и лог требуют операторскую
сессию. Рестарт запрещён при активной миссии, устаревшем состоянии или отсутствии доказательства,
что стек остановлен. После 202 наблюдайте stack/status: завершение запроса не равно готовности ROS.

Старые `/api/*` и `/ros/*` маршруты удалены. Хост, ROS-пакеты и UI обновляются одной версией
при остановленной миссии. Порты kiosk сохранены: панель 8089, рендерер лица 8090.

## Проверки

```bash
python3 -m pytest scripts/jetson-launcher/tests -q
node --test reference_ui/test/*.test.js
# После source install/setup.bash:
colcon test --packages-select guide_robot_api guide_robot_face guide_robot_mission_control
```

Тесты используют локальные HTTP/WS-сокеты и ROS-моки, без оборудования.
Независимый клиент: `python3 examples/api/client.py --tour expo_one --stop-after 10`.
Перед аппаратной приёмкой проверить Firefox/WebKit kiosk, RFID, видео Range,
обычный тур, остановку, отрицательное подтверждение с возвратом, потерю связи и безопасный рестарт.

### Потеря локализации

`state.robot.localization` и снимки/изменения WebSocket содержат состояние
initializing/ready/lost/recovering, `reason`, `motion_blocked`, `reset_required`,
`fresh`, `age_s`, `position_sigma` (метры), `yaw_sigma` (радианы) и пороги.
Команда движения при зафиксированном запрете получает HTTP 409,
`error.code=localization_required`. Активная миссия завершается с
`result.detail=localization_lost`. Сброс `/localization/reset` подтверждает только
публикацию позы и очистку костмапов: дождитесь localization.state=ready и
motion_blocked=false перед отдельным запуском новой миссии.
