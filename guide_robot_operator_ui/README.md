# guide_robot_operator_ui

Мост ROS ↔ HTTP для `guide-launcher`. Нода `operator_ui_node` слушает `127.0.0.1:8091`
(внутри контейнера, `network_mode: host`, поэтому виден с хоста) и **не имеет своей страницы**:
страница, промо, вход по PIN/RFID, текущий тур и управление стеком живут в хостовом сервисе
`guide-launcher` (`scripts/jetson-launcher/README.md`). Экран большого дисплея —
`docs/kiosk_operator_ui_explained.md`.

Пакет **не** второй клиент Nav2: движение идёт только через сервисы `mission_fsm`
(`~/request_stop`, `~/go_home`, `RunTour`). Единственное исключение — `/initialpose` при сбросе
локализации, это не движение. `/admin_cmd_vel` в пакете не используется вообще.

## Запуск

Обычно ничего запускать не нужно: нода входит в `robot.launch.py`
(`hardware.launch.py` + `operator_ui.launch.py`), который поднимает `start_stack.sh` по кнопке
«Запустить» в меню launcher'а. Отдельно:

```bash
ros2 launch guide_robot_operator_ui operator_ui.launch.py
```

Без `bridge_token_file` нода **не стартует** (см. ниже). Требует `mission_fsm`/`location_server`
для полной функциональности, но поднимается и без них: команды отвечают 409/503, сервисы
подхватываются по мере появления, без перезапуска ноды.

## Токен моста и `X-Operator`

Командные роуты принимают только запросы с заголовком `X-Bridge-Token`, равным общему секрету
(сравнение `hmac.compare_digest`). Без токена или с неверным — `403 {"error":"forbidden"}`,
причём **до** проверки тела и состояния робота (не 400, не 409). Открытые GET-роуты токена не
требуют. Заголовок `X-Operator` (id из сессии launcher'а: имя карты, `pin` или `public`) нода
пишет в свой лог вместе с путём и статусом; собственного журнала команд у ноды нет — его ведёт
launcher.

Секрет лежит в файле, параметр `bridge_token_file`. Файл один, путь к нему два (репозиторий
смонтирован в контейнер):

| Кто | Путь |
|---|---|
| нода (контейнер), параметр `bridge_token_file` | `/home/fabian/ros2_ws/src/.guide_launcher/bridge_token` |
| launcher (хост), `bridge_token_file` в `/etc/guide-launcher/config.yaml` | `~/Desktop/Projects/Robo-guide/.guide_launcher/bridge_token` |

Файл создаёт `scripts/jetson-launcher/install.sh` (если его нет; существующий не
перезаписывается). Каталог `.guide_launcher/` в `.gitignore` и содержит `COLCON_IGNORE`. Пустой,
отсутствующий или нечитаемый файл, а также пустой параметр — нода пишет `fatal` и завершается с
ошибкой. Права `0640`, если UID:GID `jetson` на хосте совпадает с `fabian` в контейнере, иначе
`0644` (токен защищает от посторонних процессов на loopback, а не от пользователей робота).

Для ноутбука: `config_dev/operator_ui_dev.yaml` (не устанавливается colcon'ом) с токеном из
`~/.guide_launcher/bridge_token`.

## Параметры

См. `config/operator_ui.yaml`. Имена сервисов/экшена `mission_fsm`/`location_server`/
`content_server`/costmap-очистки все параметризованы, не хардкожены.

| Параметр | Дефолт | Смысл |
|---|---|---|
| `bind_host` / `http_port` | `127.0.0.1` / `8091` | 8090 занят `guide_robot_face` |
| `bridge_token_file` | `""` (обязателен) | общий секрет с launcher'ом |
| `media_root` | `<share guide_robot_semantic_map>/content/media` | файлы медиа экспонатов (`/media/*`) |
| `service_timeout_s` | `5.0` | единственный источник 503 на ROS-вызовах |
| `mission_state_stale_s` | `3.0` | старше — состояние `mission_fsm` устарело |
| `initialpose_settle_s` | `0.5` | пауза после `/initialpose` перед очисткой костмапов |
| `reset_covariance_xyyaw` | `[0.25, 0.25, 0.0685…]` | ковариация начальной позы |
| `tours_language` / `content_language` | `ru` / `ru` | языки списка туров и контента |
| `run_tour_action`, `request_stop_service`, `go_home_service`, `list_tours_service`, `list_locations_service`, `clear_global_costmap_service`, `clear_local_costmap_service`, `get_exhibit_content_service`, `get_exhibit_media_service` | см. yaml | имена ROS-интерфейсов |

Вход (PIN/RFID/сессии), промо, `slide_interval_s`, `web_root` и `command_log_dir` из ноды
**удалены**: они переехали в launcher (`operator_pin`, `auth_backends`, `rfid_*`,
`session_ttl_s`, `promo_dir`, `slide_interval_s`, `always_promo`, `state_dir`).

## HTTP API

| Метод | Путь | Назначение | Токен |
|---|---|---|---|
| GET | `/ws` | push состояния (кадры, см. ниже) | нет |
| GET | `/api/tours` | `{"tours":[{"id","name"}]}` | нет |
| GET | `/api/media/<id>` | манифест слайдов экспоната | нет |
| GET | `/media/*` | статика `guide_robot_semantic_map/content/media` | нет |
| POST | `/api/tour/start` | `{"tour_id": "..."}` → `RunTour` (флаги `greet/narrate/confirm_between_stops/return_home` = True зашиты в ноде) | **да** |
| POST | `/api/tour/stop` | `request_stop` | **да** |
| POST | `/api/go_home` | `go_home` | **да** |
| POST | `/api/localization/reset` | `{"confirm": true}`: `/initialpose` в точке `home` + очистка костмапов | **да** |
| POST | `/api/costmaps/clear` | очистка global+local костмапов | **да** |

Коды: `200` успех, `400` невалидное тело, `403` нет/неверный токен, `409` команда отклонена по
состоянию робота (`mission_state_stale`, `tour_active`, `home_location_missing`, `rejected`,
`message` в теле), `503` ROS-сервис недоступен/таймаут (`service_timeout_s`; недоступность и
медленность неразличимы намеренно). Очистка костмапов: 200 либо 503 при таймауте сервиса.
Браузер к этим роутам напрямую не ходит: GET-роуты отдаёт прокси launcher'а `/ros/*`, POST —
`/api/op/*` после входа.

### Кадры `/ws`

Сервер → клиент, JSON. Кадр приходит на каждое сообщение `/mission/state`, `/supervisor/estop`,
`/supervisor/state` **и heartbeat-кадром раз в 1 с** (таймер ноды пересобирает кадр, поэтому
`mission_state_age_s` растёт, а молчащий мост отличим от живого; launcher считает кадр старше 3 с
отсутствующим). Ключи: `seq`, `stamp`, `state`, `state_name` (`idle`, `greeting`, `navigating`,
`narrating`, `answering`, `awaiting_confirm`, `paused`, `held`, `returning`, `unknown`),
`tour_id`, `stop_index`, `stop_total`, `stop_id`, `exhibit_id`, `next_exhibit_id`,
`chunk_index`, `chunk_total`, `paused_reason`, `estop`, `supervisor_state`,
`mission_state_age_s` (`null`, пока `/mission/state` ни разу не приходил). Явного поля «связь с
mission_fsm» нет: это `mission_state_age_s` не `null` и не больше 3.

## RFID

Ридер (ESP32-S3 + RC522), прошивка и правило udev остаются в этом пакете
(`firmware/rfid_bridge/`, `firmware/RFID_README.md`, `scripts/99-guide-robot-rfid.rules`), но
`/dev/rfid0` **всегда принадлежит launcher'у**: серийный порт открывает он, HMAC
challenge-response проверяет он (`rfid_port`, `rfid_secret_file` в конфиге launcher'а).
Настройка секрета и известные ограничения (MIFARE Classic клонируется, потолок стойкости — PIN) —
`firmware/RFID_README.md` и `scripts/jetson-launcher/README.md`. Ноде `pyserial` больше не
нужен.

## Промо

`promo/promo.yaml` и `promo/media/` остаются в пакете **как источник** (не устанавливаются
colcon'ом): launcher читает их прямо из чекаута (`promo_dir`) и перечитывает при смене mtime.
Формат и способ добавить картинку — `docs/kiosk_operator_ui_explained.md`, раздел 5.

## Тесты

```bash
cd guide_robot_operator_ui && python3 -m pytest test -q --ignore=test/test_state_frame.py
```

`test_state_frame.py` требует собранный `guide_robot_msgs` (запускать в контейнере после
`colcon build`). Локально нода не импортируется (нет `rclpy`), поэтому тесты покрывают
`lib/ui_server.py`: 403 без токена и с неверным токеном, 200 с верным для каждого командного
роута, открытые GET, `costmaps/clear`, охранный тест «каждый POST `/api/` в `COMMAND_PATHS`».
Тесты launcher'а — `python3 -m pytest scripts/jetson-launcher/tests -q` из корня репозитория.

Модули `lib/auth.py`, `session.py`, `rfid_link.py`, `command_log.py`, `promo_io.py` и страница
`web/` перенесены в `scripts/jetson-launcher/` (`git mv`, история сохранена).
