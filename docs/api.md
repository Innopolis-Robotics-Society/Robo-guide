# API Robo-guide

Описание реализации на **7 ноября 2026 года**. Основной интерфейс клиента —
HTTP/WebSocket API v1 хостового сервиса `guide_launcher`. Ниже также описаны
приватный ROS-мост, необязательные маршруты штатной панели и сервер лица.
Архитектурный контекст: [architecture.md](architecture.md).
Подготовка данных и голоса: [экскурсии, контент, LLM и TTS](tours-content-llm-tts.md).

Источники поведения — регистрация маршрутов и исполняемые обработчики:
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py),
[api.py](../scripts/jetson-launcher/guide_launcher/api.py),
[command_adapter.py](../guide_robot_api/guide_robot_api/command_adapter.py)
и связанные с ними реализации, указанные в разделах. Наличие вспомогательного
метода или комментария само по себе не означает наличие HTTP-маршрута.
[OpenAPI](../scripts/jetson-launcher/guide_launcher/spec/openapi.json) и
[JSON Schema событий](../scripts/jetson-launcher/guide_launcher/spec/events.schema.json)
полезны для клиентов, но перечисленные в конце особенности уточняют фактическую
реализацию. Здесь описаны defaults исходников, а не содержимое локальной
конфигурации работающего робота.

Быстрая навигация: [авторизация](#2-авторизация),
[состояние](#3-доступность-и-состояние), [операции](#7-операции-и-идемпотентность),
[миссии](#8-запуск-тура-и-поездки), [системные настройки](#13-системный-звук-wi-fi-и-питание),
[приватный мост](#16-приватный-ros-мост-на-8091), [ошибки](#18-ошибки-и-таймауты-публичного-api).

## 1. Адреса и общие правила

| Сервис | Адрес по умолчанию | Назначение |
|---|---|---|
| Host API | `http://127.0.0.1:8089/api/v1` | Внешний контракт для UI и независимых клиентов |
| Host WebSocket | `ws://127.0.0.1:8089/api/v1/events` | Снимки состояния и операций |
| ROS-мост | `http://127.0.0.1:8091` | Внутренний транспорт хостового сервиса |
| Сервер лица | `http://127.0.0.1:8090` | Страница лица и отдельный WebSocket рендера; конфигурация сервера слушает `0.0.0.0` |

**Все сокращённые пути в разделах 2–15 относятся к `/api/v1`.** Для запросов
с JSON-телом используйте `Content-Type: application/json`. В примерах
`<...>` обозначает значение, полученное клиентом или выбранное из каталога;
это не реальные идентификаторы или результаты работы робота.

Host API допускает только loopback-имена в `Host`: `127.0.0.1`, `localhost`,
`::1`. `Origin` может отсутствовать, совпадать с текущим origin либо входить
в явно заданный `allowed_origins`. Настройки допускают только конкретные
loopback origins HTTP/HTTPS. Отклонения дают `403`, причины `host_not_allowed`
или `origin_not_allowed`.

Для допустимого origin сервер возвращает `Access-Control-Allow-Origin` с его
точным значением и `Access-Control-Allow-Credentials: true`. В preflight
объявлены методы `GET, POST, OPTIONS` и заголовки `Content-Type, Idempotency-Key`.
Middleware отвечает `204` на `OPTIONS` после проверки Host/Origin, не проверяя
существование конкретного прикладного маршрута.

Каждый ответ получает `X-Request-ID`. Обычные ответы имеют `Cache-Control:
no-store`; успешно проксируемые файлы и PNG карты — `private, max-age=30`.
У потокового ответа, уже начатого до разрыва upstream, соединение может быть
закрыто без нового JSON-ответа об ошибке.

Валидаторы команд отклоняют посторонние поля и JSON, отличный от объекта.
Строки проверяются на непустое содержимое после `strip`, но возвращаются без
автоматического удаления пробелов. Числа должны быть конечными; `true/false`
не считаются числами. Эти проверки применяются там, где обработчик вызывает
валидатор; единой проверки всех тел по OpenAPI на сервере нет.

JSON-каталоги не передают query-параметры в ROS: `?language=...`, `?category=...`,
`?near_only=...` не становятся фильтрами запроса. Язык задаётся параметрами
ROS-адаптера. Пагинация каталогов и списка операций не реализована.

Источники: [http_policy.py](../scripts/jetson-launcher/guide_launcher/http_policy.py),
[validation.py](../scripts/jetson-launcher/guide_launcher/validation.py),
[bridge.py](../scripts/jetson-launcher/guide_launcher/bridge.py),
[config.py](../scripts/jetson-launcher/guide_launcher/config.py).

## 2. Авторизация

Авторизация основана на cookie `gl_session`, а не на Bearer token. Cookie
устанавливается с `HttpOnly`, `SameSite=Strict`, `Path=/`. В JSON токен сессии
не возвращается. Сервер хранит одну активную операторскую сессию: новый успешный
вход вытесняет предыдущую.

Срок сессии по умолчанию — `session_ttl_s=600` секунд. Успешный (`status < 400`)
POST через авторизационную обёртку продлевает срок; GET и отказ его не продлевают.
Срок проверяется монотонными часами, а `expires_at` в ответах — Unix time
в секундах. Перезапуск host API теряет сессии и nonce.

### POST `/auth/challenge`

Без сессии. Тело не используется. Ответ `200`:

```json
{"nonce":"<одноразовая строка>","backends":["pin"]}
```

Список `backends` определяется готовностью настроенных способов входа;
возможны `pin` и `rfid`. Nonce действует 30 секунд. RFID появляется в списке,
только когда доступен backend с портом и загруженным секретом.

### POST `/auth/verify`

Без сессии. Для PIN:

```json
{"nonce":"<из challenge>","backend":"pin","pin":"<PIN оператора>"}
```

Для RFID достаточно `{"nonce":"<из challenge>","backend":"rfid"}`:
ридер на роботе опрашивает сервер; подпись и идентификатор карты клиент
не передаёт. Backend проверяет HMAC-SHA256 ответа ридера.

Ответ `200`: `{"operator":"<имя>","expires_at":<Unix time>}` и `Set-Cookie`.
Для PIN имя оператора равно `pin`, для RFID — логическому имени из ответа карты.

После проверки непустых строк `nonce` и `backend` nonce расходуется до проверки
учётных данных и блокировки. Для следующей попытки нужен новый nonce.

| Случай | HTTP | `error.message` после middleware |
|---|---|---|
| Нет корректных строк `nonce`/`backend` | 400 | `invalid_body` |
| Nonce неизвестен, истёк или уже использован | 401 | `invalid_nonce` |
| Неверные данные или недоступный выбранный backend | 401 | `invalid_credentials` |
| В RFID-ридере нет карты | 401 | `rfid_no_card` |
| Активна блокировка попыток | 429 | `locked_out` |

После 10 неудачных проверок backend включается общая блокировка на 60 секунд;
десятая неудача ещё возвращает `401`, следующая проверка — `429`.
`rfid_no_card` не увеличивает этот счётчик. Для `429` выставляется заголовок
`Retry-After`; исходное поле `retry_after_s` не сохраняется в нормализованном
JSON ошибки. Успешный вход сбрасывает счётчик.

### GET `/auth/status`

Без обязательной сессии; проверяет переданную cookie. Ответ `200`:
`{"active":true,"expires_at":<Unix time>,"operator":"<имя>}` либо
`{"active":false,"expires_at":null,"operator":null}`.

### POST `/auth/logout`

Без обязательной сессии. Ответ `200`, `{"ok":true}` и удаление cookie.
Завершается только сессия, соответствующая переданной действительной cookie;
чужая или устаревшая cookie не завершает текущую сессию другого клиента.

Источники: `Api._authed`, `auth_*` в
[api.py](../scripts/jetson-launcher/guide_launcher/api.py),
[session.py](../scripts/jetson-launcher/guide_launcher/session.py),
[auth.py](../scripts/jetson-launcher/guide_launcher/auth.py).

## 3. Доступность и состояние

### GET `/health`

Без сессии. Ответ `200`: `{"alive":true,"instance_id":"<UUID host API>"}`.
Проверяет существование host API; доступность ROS, Nav2 и миссии из него
не следует.

### GET `/state`

Без сессии. Возвращает полный снимок `State`:

| Поле | Содержание |
|---|---|
| `instance_id` | UUID текущего экземпляра host API |
| `stack` | `{state, checks, control, last_error}`; описание в разделе 12 |
| `transport_connected` | Есть ли WebSocket-соединение host API с ROS-мостом |
| `data_fresh` | Есть ли кадр моста не старше 3 секунд |
| `robot` | Кадр состояния робота или `null`, если свежего кадра нет |
| `operations` | Все сохранённые публичные записи операций этого экземпляра |

Свежее соединение и свежие данные — разные признаки. Возраст последнего
`/mission/state` продолжает увеличиваться и на хосте после получения кадра.
`readiness` внутри `robot` отражает обнаружение ROS action/service servers,
а не гарантирует принятие конкретной команды.

### Поля `robot`

| Поле | Тип и значение |
|---|---|
| `bridge_instance_id` | Строка UUID процесса ROS-моста |
| `seq`, `stamp` | Счётчик кадра моста и его монотонное время в секундах; `stamp` не Unix time |
| `state`, `state_name` | Числовое и текстовое состояние миссии |
| `base_state_name` | Состояние под прерыванием, либо `unknown` |
| `moving` | `true` для `navigating`, `returning`, а также `answering` с базовым `navigating` |
| `tour_id` | Идентификатор тура, строка |
| `stop_index`, `stop_total`, `stop_id` | Индекс, число остановок и ID текущей точки |
| `exhibit_id`, `next_exhibit_id` | Текущий и следующий контент |
| `chunk_index`, `chunk_total` | Прогресс частей рассказа |
| `paused_reason` | `0` — нет, `1` — пользователь, `2` — safety, `3` — присутствие |
| `estop`, `supervisor_state` | Булев флаг программной остановки и строка supervisor |
| `mission_state_age_s` | Возраст получения `/mission/state`, либо `null`, если сообщения ещё не было |
| `available_commands` | Массив подсказок команд, сформированный FSM и дополнительно отфильтрованный host API |
| `readiness` | Булевы `mission`, `pose`, `speech`, `catalog`, `face` |
| `face` | `null` либо `{state, gaze_az, seq, source}` из `FaceState` |
| `speech` | `null` либо сериализованный `SpeakingStatus`, перечисленный ниже |
| `map` | Метаданные карты и положение, как у GET `/map` |
| `localization` | Состояние AMCL guard, описанное в разделе 6 |

Состояния миссии: `-1/unknown`, `0/idle`, `1/greeting`, `2/navigating`,
`3/narrating`, `4/answering`, `5/awaiting_confirm`, `6/paused`, `7/held`,
`8/returning`. Отдельного HTTP-состояния миссии `localization_lost` в этой
таблице нет: потеря локализации представлена в объекте `localization` и
результате задания.

`speech` содержит `stamp`, `speaking`, `epoch`, `goal_id`, `priority`, `scope`,
`expected_end`, `interruptible`. ROS-времена `stamp`/`expected_end` имеют форму
`{sec, nanosec}`. `expected_end` — оценка, не гарантия окончания речи.
Мост передаёт последний полученный `speech`/`face`; отдельной фильтрации их
возраста в `State` нет.

При неизвестном или превышающем 3 секунды возрасте миссии host API очищает
`available_commands`. При `localization.motion_blocked=true` он убирает
команды движения. Список не заменяет проверку при исполнении и не является
полным перечнем всех HTTP-маршрутов.

Источники: `Events.snapshot` в
[events.py](../scripts/jetson-launcher/guide_launcher/events.py),
`FrameWatcher.latest` в [bridge.py](../scripts/jetson-launcher/guide_launcher/bridge.py),
[state_frame.py](../guide_robot_api/guide_robot_api/lib/state_frame.py),
`_push_frame` в [api_node.py](../guide_robot_api/guide_robot_api/api_node.py),
[SpeakingStatus.msg](../guide_robot_msgs/msg/SpeakingStatus.msg).

## 4. WebSocket `/events`

Без сессии. После подключения сервер отправляет `snapshot`, затем фоновая
задача примерно раз в секунду отправляет `state` при изменении снимка либо
`heartbeat`. Каждый из трёх типов содержит **полный State**, включая операции:

```json
{"type":"snapshot","instance_id":"<UUID host API>","seq":1,"data":{}}
```

В этом сокращённом примере вместо `{}` в `data` находится объект из раздела 3.
Сервер не отправляет отдельные события вида `operation_completed` или отдельный
поток каждого ROS feedback: изменения объединяются в снимках.

`seq` увеличивается на уровне экземпляра API. Пропуск номера допустим,
например из-за отправки начального snapshot другому клиенту. При переподключении
выдаётся новый snapshot; журнала повторной доставки по `seq` нет. При смене
`instance_id` старые операции принадлежат прежнему экземпляру.

Входящие пользовательские WS-сообщения не исполняют команд и игнорируются.
Для команд используется HTTP POST. Transport heartbeat aiohttp настроен на
20 секунд и отличается от прикладного JSON `heartbeat`. Отправка медленному
клиенту ограничена таймаутом 1 с; при соответствующей ошибке он отключается.

Источник: `Events.ws`, `run`, `envelope`, `_send` в
[events.py](../scripts/jetson-launcher/guide_launcher/events.py).

## 5. Каталоги и медиа

Все запросы этого раздела доступны без сессии. Они обращаются через host API
к ROS-мосту, поэтому могут завершиться ошибкой транспорта или недоступности ROS.

| Маршрут | Успешное тело | Реальный запрос в ROS |
|---|---|---|
| GET `/tours` | `{tours: Tour[]}` | `ListTours`, язык `tours_language`, default `ru` |
| GET `/locations` | `{locations: Location[]}` | `ListLocations` без фильтров; по правилам каталога — публичные точки |
| GET `/content/{id}` | `{chunks, title, kind, version}` | `GetExhibitContent(exhibit_id=id, mode="full", language=content_language)` |
| GET `/media/{id}` | `{title, chunk_ids, items}` | Объединение `GetExhibitContent(mode="full")` и `GetExhibitMedia` |
| GET `/media-files/{tail}` | Файл | Проксирование `/media/{tail}` ROS-моста |
| GET `/localization/presets` | `{presets: Location[]}` | Объединение публичных и `category="charging"` локаций с отбором по настроенным ID |

Структуры:

- `Tour`: `id`, `name`, `duration_min_estimate`, `stops`, `transit_content_id`.
  Элемент `stops`: `location_id`, `exhibit_id`, `dwell_s`, `mode`.
- `Location`: `id`, `aliases` (массив строк), `pose`, `zone`, `category`,
  `is_public`. `pose` — ROS `PoseStamped`: `header:{stamp:{sec,nanosec},frame_id}`
  и `pose:{position:{x,y,z},orientation:{x,y,z,w}}`.
- Элемент `chunks`: `chunk_id`, `text`, `interruptible`, `pause_after_s`.
  В `/content/{id}` нет отдельного массива `chunk_ids`.
- Элемент `items` манифеста `/media/{id}`: `id`, `chunk_id`, `kind`, `path`,
  `duration_s`, `caption`. `path` — путь внутри каталога медиа; файл читается
  через `/media-files/{tail}`. `chunk_ids` задаёт порядок частей полного текста.

**Неизвестный контент не обязательно означает `404`.** При доступном
`content_server` GET `/content/{id}` может вернуть `200` с `chunks:[]` и пустыми
`title/kind/version`, если контент/язык не найден или узел не активен.
GET `/media/{id}` требует `media.ok`, непустые title и chunks; иначе он возвращает
`503 content_unavailable`. Успешный манифест кэшируется в ROS-мосте на 30 секунд.
Пустой список медиа допустим, если остальные условия выполнены.

Default-список presets: `parking`, `home`, `entrance`, `robo_guide`,
`expo_meeting`, `charging`. В панели карты три быстрые позиции:
«Начало экскурсии» (`expo_meeting`), «Стоянка» (`parking`), «Подзарядка» (`charging`).
Кнопка выбирает позицию; отдельное подтверждение отправляет localization/reset
с preset_id и текущей map_version. Это указание положения, без команды поездки.
В ответ включаются только найденные ID в порядке настройки; существование
каждого из них не гарантируется.

Файловый прокси передаёт `Range`, `If-None-Match`, `If-Modified-Since` и
возвращает соответствующие файловые заголовки, включая `Content-Range`,
`Accept-Ranges`, `ETag`, `Last-Modified`, если их дал upstream. Для файлов
возможны `206` и `304`. Путь с сегментом `..` или обратной косой чертой
отклоняется `404`. Ошибки HTTP от файлового upstream получают сообщение
`media_unavailable` с исходным HTTP-статусом.

Источники: `CommandAdapter.dispatch`, `_service` в
[command_adapter.py](../guide_robot_api/guide_robot_api/command_adapter.py),
`_on_api_media` в [api_node.py](../guide_robot_api/guide_robot_api/api_node.py),
[location_server.py](../guide_robot_semantic_map/guide_robot_semantic_map/location_server.py),
[content_server.py](../guide_robot_semantic_map/guide_robot_semantic_map/content_server.py),
[ROS-типы](../guide_robot_msgs/).

## 6. Карта и локализация

### GET `/map` и GET `/map/image`

Без сессии. `/map` возвращает `200` и:

| Поле | Содержание |
|---|---|
| `available` | Есть ли принятая валидная `OccupancyGrid` |
| `version` | SHA-256 метаданных и ячеек карты; применяется для проверки команд |
| `width`, `height` | Размеры сетки в ячейках |
| `resolution` | Метры на ячейку |
| `frame` | `header.frame_id` карты |
| `origin` | `{x,y,yaw}` начала сетки в системе карты |
| `robot_pose` | `null` либо `{x,y,yaw,age_s,fresh}` из TF `frame → base_footprint` |
| `target` | `null` либо `{x,y,yaw}` последней опубликованной `/mission/target_pose` |

При отсутствии карты тело сокращается до `{"available":false,"robot_pose":null}`.
Отсутствующий TF даёт `robot_pose:null`; TF свежий при `0 <= age_s <= 3`.
Наличие карты само по себе не означает готовность навигации. `target` — именно
наблюдение `/mission/target_pose`, а не обещание цели для любого вида миссии.

`/map/image` отдаёт PNG текущей карты и `ETag=version`; при отсутствии карты —
`503` (публичный файловый прокси сообщает `media_unavailable`). PNG имеет
`width × height`, неизвестная клетка — серый 160, известная —
`round(255 × (1 − occupancy/100))`. Строки сетки развёрнуты по вертикали.
Сам PNG не содержит позы робота или цели.

В координатах API `x/y` измеряются в метрах, `yaw` — в радианах.
Поза робота относится к `base_footprint`, чей `+X` направлен к роликам.
API не добавляет π к задаваемому yaw. Проверка попадания в сетку учитывает
поворот `origin.yaw`.

### Объект `robot.localization`

Обычный снимок guard содержит `state`, `reason`, `position_sigma`, `yaw_sigma`,
`position_limit`, `yaw_limit`, `age_s`, `fresh`, `motion_blocked`,
`reset_required`, `heartbeat_monotonic`. Состояния: `initializing`, `ready`,
`lost`, `recovering`. Линейные величины — метры, угловые sigma/limit — радианы.
Sigma и возраст могут быть `null`, пока оценки нет.

Если ROS-мост не получил состояние guard за 0,5 с, он подставляет сокращённый
объект: `state="lost"`, `reason="monitor_unavailable"`,
`motion_blocked=true`, `reset_required=true`, `fresh=false`.
Поля sigma/limit/age/heartbeat в таком объекте отсутствуют.

`ready` не равнозначно разрешению движения: устаревшая оценка закрывает фильтр.
Команда движения при `ready`, `fresh=false`, `reset_required=false` допускается
хостом к ROS для запроса свежей оценки перед принятием. При другой известной
блокировке host API отвечает `409 localization_required`.

### POST `/localization/reset`

Требует сессии. Возвращает сервисный ответ, а не операцию. Допустимые поля:

| Поле | Проверка |
|---|---|
| `confirm` | Обязательно именно `true` |
| `pose` | Необязательный объект с обязательными числовыми `x,y,yaw`, каждый в `[-1000000,1000000]` |
| `map_version` | Обязательная непустая строка длиной до 128 при передаче `pose` |
| `preset_id` | Необязательная непустая строка до 128; нельзя одновременно с `pose` |

Варианты: `{confirm:true, map_version, pose}`, `{confirm:true, preset_id}`
или `{confirm:true}`. В последнем случае выбирается первый доступный preset.
Для preset можно передать `map_version`; если поле отсутствует, берётся
текущая версия карты. Версия проверяется и в адаптере, и на стороне mission.
Позиция должна попадать в карту; требование свободной клетки при reset
отключено. Проверяется совпадение frame preset с frame карты.

ROS-владелец резервирует общий слот mission, запрашивает подготовку guard,
публикует `/initialpose`, пытается очистить global/local costmaps и сообщает
результат guard. При принятии HTTP `200` имеет форму:

```json
{"accepted":true,"response":{"accepted":true,"costmaps_cleared":true,"message":"pose_published"},"state":{}}
```

`state` здесь сокращён; фактически возвращается полный State. Возможен `200`
с `response.accepted=true`, `costmaps_cleared=false` и сообщением
`pose_published_costmaps_unconfirmed`: поза опубликована, очистка не подтверждена.
Ни один из этих ответов не подтверждает схождение AMCL. Итог восстановления
наблюдается через `robot.localization`; при таймауте HTTP возможен `504`.

Отказы включают `preset_or_map_missing`, `preset_frame_mismatch`, `map_changed`,
`outside_map`, `mission_busy_or_unsafe`, `localization_monitor_unavailable`.
Без явного reset действует отдельная настроенная в ROS проверка парковки при
старте; она не является HTTP-командой.

Источники: [map_adapter.py](../guide_robot_api/guide_robot_api/map_adapter.py),
[map_geometry.py](../guide_robot_mission_control/guide_robot_mission_control/lib/map_geometry.py),
[pose_control.py](../guide_robot_mission_control/guide_robot_mission_control/pose_control.py),
[localization_policy.py](../guide_robot_navigation/guide_robot_navigation/localization_policy.py),
[localization_guard.py](../guide_robot_navigation/guide_robot_navigation/localization_guard.py).

## 7. Операции и идемпотентность

Операцию возвращают POST `/missions`, `/missions/go-to`, `/missions/go-to-pose`,
`/speech`, `/wifi/connect`, `/system/shutdown`, `/system/reboot`.
Нужен заголовок `Idempotency-Key`: непустая строка длиной до 128.
Ответ `202` означает резервирование операции, а не её принятие ROS или завершение.

| Поле Operation | Значение |
|---|---|
| `id` | UUID операции |
| `instance_id` | UUID host API |
| `kind` | `missions`, `missions/go-to-pose`, `speech`, `wifi`, `system_power` |
| `status` | `pending`, `running`, `succeeded`, `failed`, `canceled`, `rejected`, `unknown` |
| `feedback`, `result` | `null` либо объект, зависящий от вида операции |
| `created_at` | Unix time резервирования |
| `goal_id` | Необязательный массив байтов UUID ROS goal после принятия |
| `updated_at` | Необязательный Unix time обновления из ROS-реестра |

`/missions/go-to` нормализуется в `kind="missions"`. Для Wi-Fi и питания
ROS goal отсутствует. Внутренние поля с префиксом `_`, включая владельца
и payload, из публичной записи удаляются.

Ключ действует на пару **сессия + ключ** внутри экземпляра host API и общий
для всех видов операций. Повтор того же нормализованного запроса возвращает
прежнюю операцию; другой payload или kind с тем же ключом — `409
idempotency_conflict`. Лимит всего реестра — 256 записей, включая завершённые.
Ключи не вытесняются: новая операция при заполнении даёт `409 operation_capacity`.
После рестарта реестр теряется; повтор команды в новом экземпляре может создать
новое действие. Общие проверки маршрута выполняются и для повторного POST,
поэтому при переходе стека/сети/питания повтор может получить отказ до поиска ключа.

`pending` и `running` — незавершённые состояния. Остальные терминальные;
последующий snapshot не меняет уже терминальную запись на хосте.
Для ROS-операции `unknown` устанавливается при потере свежего кадра,
переподключении upstream, смене экземпляра моста, исчезновении соответствующего
action server либо неопределённом результате отправки. HTTP upstream
`500/502/504` переводит отправляемую операцию в `unknown`; другие ответы
`>=400` — в `rejected`. Операции не переотправляются автоматически.

### GET `/operations/{id}`

Требует любой действительной операторской сессии; возвращает `200 Operation`
или `404`. Проверки, что читатель создал операцию, нет. Кроме того, публичные
поля всех операций уже доступны без сессии через `/state` и `/events`.

### POST `/operations/{id}/cancel`

Требует именно сессию-создателя; тело не используется. Чужой владелец — `403
operation_owner_required`, отсутствующий ID — `404`, терминальный статус или
`kind="system_power"` — `409 operation_not_cancelable`.

Для ROS goal адаптер вызывает стандартную отмену action. При наличии
`goals_canceling` ответ — `202 {"accepted":true}`, иначе `409`. Принятие отмены
не заменяет итоговый result операции. У Wi-Fi собственная отмена не реализована:
общий маршрут пытается обратиться к ROS-реестру, где Wi-Fi handle отсутствует.
Гарантировать отмену `/wifi/connect` через этот endpoint нельзя.

### Feedback и result ROS-операций

| Kind | Feedback | Result при получении ROS-результата |
|---|---|---|
| `missions` | `phase`, `stop_index`, `stop_total`, `stop_id`, `eta_s` | `outcome`, `stops_completed`, `stops_skipped`, `detail` |
| `missions/go-to-pose` | `phase` | `outcome`, `detail` |
| `speech` | `clause_index`, `clause_count`, `progress`, `current_clause` | `status`, `spoken_text`, `spoken_chars`, `spoken_duration`, `message` |

`phase` использует коды MissionState. Для RunTour `outcome`: `0 completed`,
`1 canceled`, `2 aborted`, `3 no_visitor`; GoToPose формирует `0` для успеха,
`1` для отмены/shutdown, `2` для отказа. Для Say `result.status`: `0 completed`,
`1 preempted`, `2 cancelled`, `3 rejected`, `4 failed`.

Адаптер учитывает и ROS GoalStatus: `4 → succeeded`, `5 → canceled`,
`6 → failed`, остальные → `unknown`. Ненулевой outcome миссии/позы дополнительно
превращает статус в `canceled` для `1`, иначе в `failed`; для речи `1/2`
дают `canceled`, `3/4` — `failed`. В частности, `Say result.status=3` — это
`Operation.status=failed`; `Operation.status=rejected` также используется
для отказа принять сам goal. При транспортных ошибках и отказах `result`
может быть диагностическим объектом, а не штатной структурой результата.

Источники: [events.py](../scripts/jetson-launcher/guide_launcher/events.py),
`launch`, `send_operation`, `cancel_operation` в
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py),
`_action` в [command_adapter.py](../guide_robot_api/guide_robot_api/command_adapter.py),
[RunTour.action](../guide_robot_msgs/action/RunTour.action),
[GoToPose.action](../guide_robot_msgs/action/GoToPose.action),
[Say.action](../guide_robot_msgs/action/Say.action).

## 8. Запуск тура и поездки

Все команды этого раздела требуют сессию и `Idempotency-Key`, возвращают
`202 Operation`. Для новой операции необходим свежий кадр моста и соответствующий
флаг `readiness`: `mission` для тура/точки, `pose` для произвольной позы.
Недоступность до резервирования — `503 subsystem_unavailable`.

### POST `/missions`

Нужно передать ровно один селектор: `tour_id` **или** `location_ids`.

| Поле | Тип и предел | Default |
|---|---|---|
| `tour_id` | Непустая строка до 128 | Нет |
| `location_ids` | Массив от 1 до 100 непустых строк до 128 | Нет |
| `start_index` | Целое `0..65535`, индекс с нуля | `0` |
| `time_budget_min` | Целое `0..1440`; `0` без бюджета | `0` |
| `greet` | Boolean | `true` |
| `narrate` | Boolean | `true` |
| `confirm_between_stops` | Boolean | `false` |
| `return_home` | Boolean | `true` |

Пример формы: `{"tour_id":"<ID из GET /tours>","confirm_between_stops":true}`.
Сохранённое предпочтение тура в `reference/preferences` не подставляется.
Host проверяет форму; ROS mission проверяет маршрут, индекс, занятость,
активацию и локализацию. Второй goal не вытесняет активную миссию.
Отказ ROS после `202` отображается в операции. Неизвестный тур или неправильный
маршрут/индекс могут быть обнаружены уже после принятия goal: тогда result
содержит `outcome=2` и `detail="tour_not_found"`,
`"location_service_unavailable"` или `"invalid_route_or_start_index"`,
а статус операции становится `failed`.

### POST `/missions/go-to`

Тело: `{"location_id":"<ID точки>"}`. Только это поле.
Преобразуется в `/missions` с одним `location_ids`, `greet=false`,
`narrate=false`, `return_home=false`; остальные defaults те же.
Это поездка через RunTour и общий слот mission, а не отдельный Nav2-клиент UI.

### POST `/missions/go-to-pose`

Тело: `{"map_version":"<GET /map: version>","pose":{"x":0,"y":0,"yaw":0}}`.
Нулевые координаты здесь показывают форму и не являются рекомендуемой целью.
Все три числовых поля обязательны и ограничены `[-1000000,1000000]`;
`map_version` — непустая строка до 128. Дополнительных полей нет.

До ROS goal адаптер проверяет текущую версию, границы сетки и что клетка
**строго равна 0**. Неизвестная или частично занятая клетка не подходит.
Возможные причины: `map_unavailable`, `map_changed`, `outside_map`,
`target_not_free`. Проверка только клетки цели не доказывает существование
проходимого маршрута: планирование выполняет Nav2.

На стороне mission повторно проверяются карта, frame, ориентация, свежий TF,
локализация, доступность NavigateToPose и возможность занять общий слот.
Поездка исполняется без приветствия, рассказа и возврата.

Источники: `start_operation` и `launch` в
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py),
[validation.py](../scripts/jetson-launcher/guide_launcher/validation.py),
[mission_fsm_node.py](../guide_robot_mission_control/guide_robot_mission_control/mission_fsm_node.py),
[pose_control.py](../guide_robot_mission_control/guide_robot_mission_control/pose_control.py).

## 9. Команды миссии и взаимодействия

Требуют сессию. `Idempotency-Key` для этих сервисных команд не используется.
Если тело необязательно, допустимо не передавать его или передать `{}`;
посторонние поля отклоняются.

| POST-маршрут | Тело | Действие и проверка в реализации |
|---|---|---|
| `/missions/pause` | `{}` | Запрос паузы при активном контексте в `navigating`, `narrating`, `answering` |
| `/missions/resume` | `{}` | Продолжение активного контекста из `paused` |
| `/missions/stop` | `{}` | Остановка активного задания на месте; если goal ещё резервируется, ставится ожидающий stop |
| `/missions/return` | `{}` | Отдельный возврат на настроенную домашнюю позу |
| `/missions/base` | `{location_id}` | Отдельная поездка к служебной точке категории `charging` |
| `/missions/redirect` | `{location_id}` | Перенаправление активного тура к известной ему локации |
| `/interaction/confirm` | `{answer:"yes"\|"stop"\|"question"}` | Ответ в свежем `awaiting_confirm` |
| `/interaction/question` | `{}` | Запрос перехода из `awaiting_confirm` в `answering` |
| `/interaction/answer` | `{outcome:"resume"\|"skip"\|"end"}` | Завершение прерывания в `answering` |
| `/costmaps/clear` | `{}` | Последовательная очистка global и local costmaps |

`location_id` — непустая строка до 128. Redirect разрешён в `greeting`,
`navigating`, `narrating`, `answering`, `awaiting_confirm`; дополнительно
проверяются активный контекст, локализация и ID точки.
Return/base отклоняются при занятой обычной миссии. Повтор уже идущего
standalone-возврата к той же цели может вернуть `already_returning`;
другая цель — `robot_moving`.

`missions/stop` отменяет работу на месте. `interaction/confirm` с `answer="stop"`
завершает тур по ветке FSM, учитывающей возврат. Это разные команды.
`outcome="skip"` в `/interaction/answer` означает пропуск текущей остановки
по правилам FSM, а не произвольную следующую Nav2-цель.

`answer="question"` сначала вызывает `request_question`, затем best-effort
`prompt_listen`, возвращая `listening:true/false`. Прямой
`/interaction/question` вызывает только `request_question` и не открывает
дополнительно окно прослушивания этим обработчиком.

Успешные сервисные команды из таблицы возвращают `200`:

```json
{"accepted":true,"response":{},"state":{}}
```

`response` и `state` здесь сокращены. `state` — последний наблюдаемый State,
а не синхронное подтверждение нового положения/фазы. `response` зависит от команды:

| Команда | Поля `response` |
|---|---|
| pause, resume, stop, return, question | `success`, `message` из ROS Trigger |
| redirect | `accepted`, `message` |
| base | `message` (адаптер не переносит `accepted` в этот внутренний ответ) |
| confirm yes/stop | `message` |
| confirm question | `message`, `listening` |
| answer | `accepted`, `message`, `resume_token` |
| costmaps/clear | `message` |

Return/base возвращают факт принятия сервиса, без новой Operation и без
ROS action result в HTTP. Движение наблюдается через состояние миссии.
Отказ сервиса обычно даёт `409`; недоступный сервис — `503` там, где
используется `_service`, а ожидание без ответа — `504`. Часть обработчиков
сразу ожидает ROS-ответ без предварительного `service_is_ready`, поэтому
не следует обещать одинаковый код отказа для всех недоступных сервисов.

Если bridge вернул ошибку, `command` добавляет к ней State; затем middleware
оставляет нормализованную ошибку и верхнеуровневый `state`. Если отказ возник
раньше в авторизации/валидации/общей блокировке, поля `state` может не быть.

Источники: `RobotApi.command` в
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py),
`CommandAdapter.dispatch` в
[command_adapter.py](../guide_robot_api/guide_robot_api/command_adapter.py),
`_on_api_confirm`, `_on_api_go_to_base`, `_on_api_costmaps_clear` в
[api_node.py](../guide_robot_api/guide_robot_api/api_node.py),
[FSM](../guide_robot_mission_control/guide_robot_mission_control/mission_fsm_node.py).

## 10. Речь и выражение лица

### POST `/speech`

Сессия + `Idempotency-Key`; ответ `202 Operation(kind="speech")`.
Требуется свежий кадр и `readiness.speech` для нового запроса.

| Поле | Проверка | Default |
|---|---|---|
| `text` | Обязательная непустая строка до 10000 | Нет |
| `voice` | Строка; отдельный предел длины не задан | `""` |
| `max_duration` | Конечное число от 1 до 300 секунд | `120` |

Host фиксирует `priority=100`, `scope=2` (dialog), `interruptible=true`.
Эти поля и `stream_id` нельзя переопределять через публичное тело.
Это обычная цель Say; публичного HTTP-маршрута передачи `SayStreamChunk` нет.
Результат и отмена — через операции из раздела 7.

### GET `/face/expressions`

Без сессии. Ответ `200 {"expressions":[...]}` — ключи `states` установленного
`guide_robot_face/config/face_states.yaml`, прочитанного ROS-мостом.
Клиенту следует брать допустимые значения из этого ответа.

### POST `/face/override`

Требует сессию. Поля: обязательный `state` (непустая строка до 128), обязательный
`duration_s` (`0.1..60`), необязательный `gaze_az` (`-90..90`, default `0`, градусы).
Неизвестное выражение отклоняет ROS-агрегатор.

Успех: `200 {accepted:true,response:{accepted,message,effective_state,source},state}`.
`source` — `automatic` или `override`. Ошибка и движение имеют приоритет над
override: запрос может быть принят, а `effective_state` остаться автоматическим.
По истечении монотонного срока override снимается.

### POST `/face/clear`

Требует сессию; тело не читается. Отправляет в ROS пустой state и нулевой срок.
Ответ отличается от `/face/override`: поля `accepted`, `message`,
`effective_state`, `source`, `state` расположены **на верхнем уровне**, без
обёртки `response`.

Источники: [validation.py](../scripts/jetson-launcher/guide_launcher/validation.py),
`clear_face`, `command` в [v1.py](../scripts/jetson-launcher/guide_launcher/v1.py),
`_set_override` в [aggregator_node.py](../guide_robot_face/guide_robot_face/aggregator_node.py).

## 11. Команды посетителя

Доступны без операторской сессии.

| Маршрут | Фактическое поведение |
|---|---|
| POST `/public/start-tour` | Запуск отключён: обработчик возвращает `403 operator_session_required`; тело и ключ идемпотентности он не использует |
| POST `/public/stop` | Тело не используется; stop только при `stack=UP`, свежей миссии и признаке движения |
| POST `/public/confirm` | Читает `answer=yes/stop/question`; требует включённые `visitor_confirm_buttons`, `stack=UP`, свежий `awaiting_confirm` без estop/FAULT/SHUTDOWN |

У stop и confirm отдельные общие для сервиса интервалы ограничения частоты
1,5 с. Время запроса учитывается до проверки состояния робота, поэтому
отказ по состоянию также может занять этот интервал. Проверка может вернуть
`409` с причинами `rate_limited`, `ros_down`,
`no_mission_fsm`, `not_moving`; для confirm также `disabled`, `estop`,
`not_awaiting_confirm`. Это именно HTTP `409`, а не `429`.
Проверка stop допускает `moving=true` и состояния `navigating/returning`;
estop не запрещает запрос остановки.

Успех stop: `200 {ok:true,accepted:true,state}`.
Успех confirm: `200 {ok:true,answer,listening,accepted:true,state}`.
`answer` отсутствует/неизвестен — `400 invalid_answer`. Публичный обработчик
confirm не отклоняет остальные поля объекта, а извлекает только `answer`.
При отказе upstream ниже 500 публичный обработчик возвращает `409`, при 5xx
сохраняет статус; причина имеет вид `upstream_<статус>`.

`public/start-tour` и `public/confirm` проходят общую блокировку `_controlled`:
поэтому до своего обработчика они могут получить `409` о переходе системы.
Настройка `public_tours` не разрешает публичный запуск.

Источник: `public_*`, `_public_*_refusal` в
[api.py](../scripts/jetson-launcher/guide_launcher/api.py) и регистрация в
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py).

## 12. Управление ROS-стеком

### GET `/stack/status`

Без сессии. `200 {state,checks:{container,launch,bridge},control,last_error}`.
`state` — `DOWN`, `STARTING`, `UP`, `DEGRADED` или `FAILED`.
Проверки относятся к контейнеру, процессу `ros2 launch` и `/healthz` моста;
`UP` не является проверкой готовности всех узлов ROS. `control=false` означает,
что контейнер для управления не задан; в этом режиме container/launch в
checks устанавливаются в `true`, а наблюдение основано на bridge.

### POST `/stack/start` и POST `/stack/restart`

Требуют сессию; тело и `Idempotency-Key` не используются. Действие идёт в фоне.
Обычный ответ `202 {"ok":true,"state":"STARTING"}`; это не Operation.
Результат отслеживается через `/stack/status` или `State.stack`.

Start создаёт/запускает контейнер при необходимости и вызывает настроенный
start script. Restart мягко останавливает launch, перезапускает контейнер,
затем вызывает start script. Для restart проверяется отсутствие активных
mission-операций и свежий idle либо подтверждённая остановка контейнера
не более 3 секунд назад при отсутствии launch/bridge. При наличии кадра
дополнительно вызывается ROS `/internal/missions/check-idle`.

Отказы: `400 stack_control_disabled`, `409 starting`,
`mission_operation_active`, `mission_state_stale`, `robot_moving`,
`safe_idle_not_confirmed` и общие блокировки. Потеря одного только launch-процесса
не считается подтверждённым stopped. Ошибка внешней команды может дать
`500 command_failed`; последующее состояние хранит `last_error`.

### GET `/stack/log`

Требует сессию. `200 {"lines":[...]}` — последние 30 строк настроенного
stack log из контейнера. Если чтение недоступно или управление отключено,
возвращается пустой список. Публичного `/stack/stop` в реализации нет.

Источники: [stack.py](../scripts/jetson-launcher/guide_launcher/stack.py),
`_stack_command` в [api.py](../scripts/jetson-launcher/guide_launcher/api.py),
`restart_guard`, `stack_restart` в [v1.py](../scripts/jetson-launcher/guide_launcher/v1.py).

## 13. Экран, системный звук, Wi-Fi и питание

Эти подсистемы обслуживаются хостом. Настроенные Unix-socket helpers
не являются ROS-узлами. Все маршруты раздела требуют операторскую сессию.

### GET `/display/face-cursor` и POST `/display/face-cursor`

Переключатель указателя на физическом экране с глазами. POST принимает только
`{"visible":true}` или `{"visible":false}`; GET и POST возвращают сохранённое
`{visible:boolean}`. По умолчанию указатель скрыт. Настройка сохраняется атомарно
в `<state_dir>/face-cursor-visible`; `guide-face-cursor` применяет её во время
работы и после перезагрузки. Отдельная служба `guide-face-cursor` управляет всем
экраном с глазами, включая рабочий стол; ROS и браузер могут быть остановлены.
Курсор HDMI-панели не меняется. Ошибка доступа к файлу настроек возвращает 503.

### GET `/audio/output` и POST `/audio/output`

GET читает состояние. POST допускает только необязательные `volume` (число
`0..100`) и `muted` (boolean). Пустой объект допустим. Ответ обоих запросов:
`200 {available:true,volume,muted,maximum_percent}`.

`volume` — процент разрешённого диапазона, а не прямой процент PulseAudio.
При default helper `GUIDE_AUDIO_MAX_PERCENT=20` значение API `100` соответствует
20% шкалы PulseAudio. POST сохраняет желаемые настройки и применяет их к одному
настроенному sink; helper восстанавливает их при появлении устройства.
Команда не является отдельной операцией и не требует ключа идемпотентности.

Если `audio_socket` не настроен — `503 audio_unavailable`; при ошибке связи
с helper host возвращает `504 outcome_unknown`. Сам helper может вернуть
`503 audio_unavailable`. `_controlled` для этих маршрутов не применяется.

### GET `/wifi`

Ответ `200`:

| Поле | Содержание |
|---|---|
| `available` | `true` при успешном чтении helper |
| `device` | Настроенный сетевой интерфейс |
| `state` | Числовое состояние устройства NetworkManager |
| `current_ssid` | SSID текущей точки либо `null` |
| `current_profile_id` | UUID активного подключения либо `null` |
| `ipv4_addresses` | IPv4-адреса текущего Wi-Fi-интерфейса из NM IP4Config; пустой массив без активного подключения/адреса |
| `networks` | Массив `{ssid,strength,security,sae_only}` |
| `profiles` | Массив `{id,ssid,name,last_connected}` разрешённых сохранённых профилей; `name` — имя профиля, `last_connected` — NetworkManager `connection.timestamp` (Unix seconds, `0`, если подключения ещё не было) |

`security` принимает `enterprise`, `personal`, `open`, `unsupported`.
Список `networks` выдаётся по access points NetworkManager: сервер не сортирует
его и не объединяет одинаковые SSID. `profiles` не ограничен видимыми сетями;
фильтрация/сортировка в штатной панели — отдельная клиентская логика.
На один SSID helper возвращает одни сохранённые настройки: активные, иначе
последние успешно подключавшиеся. Панель показывает одну строку на видимый SSID,
без выбора профилей, их названий и UUID. Connect/forget из панели используют SSID.
В API не возвращаются пароли профилей.

Helper работает только с настроенным Wi-Fi-адаптером. Для выбора сохранённых
профилей допускаются привязанные к нему инфраструктурные подключения
с `key-mgmt` `none`, `wpa-psk`, `sae`; hotspot, USB maintenance adapter и
enterprise-профили исключены соответствующими проверками реализации.

### POST `/wifi/scan`

Можно передать `{}`. Helper запрашивает scan и ждёт изменения `LastScan`
до 25 секунд. Успех — `200` с той же структурой, что `/wifi`; таймаут —
`504 scan_timeout`. Это синхронный HTTP-запрос, без Operation.

### POST `/wifi/connect`

Сессия + `Idempotency-Key`, ответ `202 Operation(kind="wifi")`.
Допустимые поля тела: `ssid`, `password`, `profile_id`, `hidden`.

- Для известного `ssid` используется существующее подключение. Пустой пароль
  оставляет сохранённый пароль; непустой обновляет его с сохранением UUID и
  прочих настроек. При неудаче прежние настройки и пароль восстанавливаются
  до попытки возврата к предыдущему подключению.
- Для нового SSID создаётся подключение: `ssid` — строка длиной 1–32 байта UTF-8,
  `password` — строка, default `""`. Непустой пароль должен иметь 8–63 символа
  либо ровно 64 шестнадцатеричных символа. Для найденной сети с
  `enterprise/unsupported` возвращается `unsupported_security`.
- `hidden` для нового профиля преобразуется helper через `bool(value)`;
  default `false`. На уровне host строгой проверки типа этого поля нет.

`profile_id` поддержан для совместимости: задаёт сеть существующего профиля;
если остались дубли, используются активные или последние успешные настройки.
Неизвестный UUID даёт `profile_not_found`, новое подключение не создаётся.
Старый секрет читается внутри helper только при замене пароля для возможного
отката, не передаётся в HTTP/операции/журнал и не сохраняется отдельно.

Feedback содержит `phase`: `connecting`, `authenticating`, `obtaining_address`
или `restoring`. Helper ждёт активацию до 60 итераций с паузой 1 с. При неудаче
может один раз восстановить предыдущее разрешённое инфраструктурное подключение;
hotspot не выбирается для восстановления.

Нормальный result: `{connected:boolean,restored_previous:boolean}`.
Успешное соединение → `succeeded`; отказ helper с 4xx → `failed`; 5xx/сбой
связи → `unknown`. При ошибке result также может содержать `message`.
Новая операция, зарезервированная во время занятого сетевого lock, получает
`rejected` и `result.message="network_busy"`.

Идемпотентность сравнивает параметры и HMAC-отпечаток пароля, а не сохраняет
сам пароль в реестре операции. Wi-Fi-операция помечена как хостовая: потеря ROS
сама по себе не переводит её в `unknown`.

### POST `/wifi/disconnect` и POST `/wifi/forget`

Disconnect допускает `{}` и отключает настроенное устройство.
Forget использует `{ssid:"<название сети>"}` и удаляет сохранённые настройки
этой сети, включая старые дубли. Прежний `profile_id` также принимается;
если сеть не найдена, helper отвечает `400 profile_not_found`. Успех обоих — `200` со свежим
Wi-Fi-состоянием. Операции и ключи идемпотентности здесь не используются.

Для connect/disconnect/forget проверяется свежий безопасный idle либо
недавно подтверждённый остановленный стек. При наличии кадра дополнительно
вызывается ROS check-idle. Отказ — `409 safe_idle_not_confirmed`.
Scan этой проверки не требует. Занятый сетевой lock даёт `409 network_busy`
для GET/scan/disconnect/forget. Отсутствующий `wifi_socket` — `503
wifi_unavailable`, ошибка транспорта helper — `504 outcome_unknown`.

На уровне host все Wi-Fi-запросы разрешают только имена полей
`ssid/password/profile_id/hidden`; полная проверка параметров зависит от действия
и helper. Общий маршрут POST `/wifi/{action}` принимает имена `status`, `scan`,
`connect`, `disconnect`, `forget`, прочие → `404`. Однако POST `/wifi/status`
не имеет успешной реализации в helper и доходит до `400 unsupported_action`;
для чтения используется GET `/wifi`.

### GET `/system/power`

`200 {enabled:boolean,pending:boolean,operation_id:string|null}`.
`enabled` соответствует `power_control_enabled` (default `false`).
`pending` учитывает выполняемую команду и файл `/run/guide-system-power.pending`.
`operation_id` — последняя операция питания текущего процесса, либо `null`;
поэтому при сохранённом маркере после рестарта возможны `pending=true` и
`operation_id=null`.

### POST `/system/shutdown` и POST `/system/reboot`

Оба действия зарегистрированы через POST `/system/{action}`; другие значения
`action` возвращают `404` после проверки сессии.
Сессия + `Idempotency-Key`; тело строго `{"confirm":true}`. При выключенной
возможности — `503 power_control_disabled`. Возвращается `202
Operation(kind="system_power")`. Повтор того же запроса/ключа возвращает ту же
операцию при выполняемой команде; конфликтующий новый запрос может получить
`409 system_power_pending`. Сохранённый pending-маркер также блокирует запуск.

Реализация вызывает фиксированный helper `guide-system-power poweroff|reboot`.
Feedback: `{phase:"scheduling"}`. Успешный result:
`{scheduled:true,action:"shutdown"|"reboot",delay_s:0}` и `status="succeeded"`.
Это подтверждение успешного вызова системного планирования, а не доказательство
фактического выключения. Ненулевой код helper → `failed/power_command_failed`;
таймаут с `rc=-1` → `unknown/outcome_unknown`. Ошибка создания процесса
возвращает `rc=127` и также считается `failed`.

**Проверки idle/ROS и `_controlled` для питания нет.** Запрос не ждёт текущий
переход ROS или Wi-Fi и может быть принят при движении, если авторизация,
confirm и настройка допускают его. Публичная отмена питания отсутствует.
Это фактическое поведение `PowerControl.request`, а не дополнительная гарантия
безопасной остановки миссии.

Источники: [system_settings.py](../scripts/jetson-launcher/guide_launcher/system_settings.py),
[audio_service.py](../scripts/jetson-launcher/helpers/audio_service.py),
[wifi_service.py](../scripts/jetson-launcher/helpers/wifi_service.py),
[power.py](../scripts/jetson-launcher/guide_launcher/power.py),
[guide-system-power](../scripts/jetson-launcher/helpers/guide-system-power).

## 14. Общие блокировки команд

Обёртка `_controlled` последовательно проверяет:

1. Отложенное питание: `409 system_power_pending`.
2. Известную блокировку локализации для движения: `409 localization_required`,
   кроме случая stale-ready без сохранённого latch из раздела 6.
3. Занятый lock Wi-Fi для пути вне `/wifi`: `409 network_transition_in_progress`.
4. Переход стека для пути вне `/stack/`: `409 stack_transition_in_progress`.

Она применяется к запуску mission/speech операций, большинству сервисных
команд, stack start/restart, публичным start-tour/confirm и Wi-Fi POST.
Движением для второй проверки считаются `missions`, `missions/go-to`,
`missions/go-to-pose`, `missions/return`, `missions/resume`, `missions/redirect`,
`missions/base`.

Из неё исключены `missions/stop`, публичный stop, отмена операции, `face/clear`,
audio output, reference preferences и команды питания. Это не отменяет их
собственную авторизацию и локальные проверки. Разные защитные условия могут
дать отказ раньше валидации тела или дедупликации ключа.

Источник: `RobotApi._controlled` и регистрация обработчиков в
[v1.py](../scripts/jetson-launcher/guide_launcher/v1.py).

## 15. Необязательные маршруты reference UI и документация

Следующие маршруты регистрируются, только если `serve_ui=true` и найден
каталог страницы. При `serve_ui=false` API управления продолжает работать,
а эти маршруты и `/` отсутствуют.

| Маршрут | Сессия | Ответ/поведение |
|---|---|---|
| GET `/reference/config` | Нет | `{always_promo,slide_interval_s,promo_interval_s,stack_control,visitor_confirm_buttons}` |
| GET `/reference/preferences` | Нет | `{tour_id,confirm_between_stops}` |
| POST `/reference/preferences` | Да | Обновляет переданные предпочтения, возвращает тот же объект |
| GET `/reference/promo` | Нет | `{rev,items,promo_interval_s}` |
| GET `/reference/promo-files/{tail}` | Нет | Файл из каталога промо; выход за каталог/отсутствие → `404` |

В preferences разрешены только `tour_id` и `confirm_between_stops`.
`tour_id` соответствует регулярному выражению `^[\w-]{1,64}$`, где Python
`\w` допускает Unicode-буквы/цифры и `_`; значение не проверяется по каталогу
туров. `confirm_between_stops` — boolean. Проверка всех полей предшествует
записи. Предпочтения хранятся в `state_dir/state.json` и переживают рестарт;
они не изменяют defaults POST `/missions`.

Элемент промо: `id`, `kind` (`image`/`video`), `path`, `duration_s`, `caption`.
`rev` зависит от mtime/размера `promo.yaml`, либо равен `none`.
Промо перечитывается при изменении файла; при отсутствующем/некорректном файле
может возвращаться пустой список. Манифест не гарантирует существование всех
указанных файлов.

Вне префикса `/api/v1` при тех же условиях: GET `/` — `index.html`,
GET `/static/{path}` — статика страницы. При наличии каталога `spec`
независимо от UI раздаётся GET `/api/v1/docs/{path}`, в том числе
`/api/v1/docs/index.html`, `/api/v1/docs/openapi.json` и
`/api/v1/docs/events.schema.json`.

Источники: [reference.py](../scripts/jetson-launcher/guide_launcher/reference.py),
[state.py](../scripts/jetson-launcher/guide_launcher/state.py),
[promo.py](../scripts/jetson-launcher/guide_launcher/promo.py),
[server.py](../scripts/jetson-launcher/guide_launcher/server.py).

## 16. Приватный ROS-мост на 8091

Этот раздел использует полные пути относительно `http://127.0.0.1:8091`.
Все маршруты, кроме `/healthz`, требуют `X-Bridge-Token`. Cookie `gl_session`
здесь не проверяется. Host добавляет также `X-Operator` к командам, но текущий
dispatcher ROS-моста не использует его для дополнительной авторизации.

| Метод и путь | Назначение |
|---|---|
| GET `/healthz` | `200 {alive:true}`, без проверки ROS-подсистем |
| GET `/internal/ws` | Последний кадр ROS и последующие обновления |
| GET `/internal/map`, `/internal/map/image` | Метаданные карты и PNG |
| GET `/internal/localization/presets` | Настроенные найденные Location |
| GET `/internal/tours`, `/internal/locations` | Сериализованные ROS-каталоги |
| GET `/internal/content/{id}`, `/internal/media/{id}` | Контент и объединённый манифест |
| GET `/internal/face/expressions` | Ключи конфигурации лица |
| GET `/internal/operations/{id}` | Внутренняя action-запись или `404 operation_not_found` |
| POST `/internal/missions`, `/internal/speech`, `/internal/missions/go-to-pose` | Запуск RunTour/Say/GoToPose |
| POST `/internal/operations/{id}/cancel` | Отмена ROS goal |
| POST `/internal/missions/check-idle` | Проверка фактического слота исполнения и последнего idle |
| POST `/internal/missions/pause`, `/internal/missions/resume`, `/internal/missions/stop`, `/internal/missions/return` | ROS Trigger |
| POST `/internal/missions/base`, `/internal/missions/redirect` | Сервис с `location_id` |
| POST `/internal/interaction/confirm`, `/internal/interaction/question`, `/internal/interaction/answer` | Команды взаимодействия |
| POST `/internal/face/override` | OverrideFace; пустой state снимает override |
| POST `/internal/localization/reset` | ResetLocalization через mission |
| POST `/internal/costmaps/clear` | Очистка costmaps |
| GET `/media/{path}` | Статические файлы, если каталог media существует |

Приватная регистрация `/internal/{resource:.*}` принимает методы общим
обработчиком; фактический перечень выше задаётся `dispatch`. Неизвестный
resource/метод даёт `404`, а не автоматически сгенерированный публичный контракт.
Публичный `/missions/go-to` преобразуется хостом в `/internal/missions`;
приватного отдельного go-to handler нет. Face clear аналогично преобразуется
в `/internal/face/override`.

Тело action-запроса содержит `_operation_id`, добавленный хостом, и уже
нормализованные поля goal. Мост не повторяет всю публичную валидацию и не
содержит сессий. Его запись имеет `kind="mission"|"pose"|"speech"`,
`id/status/feedback/result/updated_at` и после принятия `goal_id`.
Хост сохраняет свой публичный `kind` и `instance_id`.

Приватный реестр тоже имеет лимит 256, но, в отличие от host-реестра, может
вытеснить завершённую запись `succeeded/failed/canceled/rejected`. Публичная
идемпотентность определяется хостовым реестром, а не этим механизмом.

Приватный WS передаёт непосредственно ROS-кадр с `bridge_instance_id` и
внутренним `operations`; публичной обёртки `{type,instance_id,seq,data}` здесь
нет. На подключении отправляется последний кадр, если он уже есть. Входящие
WS-сообщения не выполняют команд.

У приватных ошибок другой формат: например `{message:"forbidden"}` с `403`,
`{message:"outcome_unknown"}` с `504`. Публичный middleware с `error.code`
и `request_id` здесь не применяется. `Cache-Control` устанавливается `no-cache`.

Основное соответствие HTTP → ROS:

| Публичная команда | ROS-интерфейс по умолчанию |
|---|---|
| missions, missions/go-to | Action `/run_tour` (`RunTour`) |
| missions/go-to-pose | Action `/mission_fsm/go_to_pose` (`GoToPose`) |
| speech | Action `/say` (`Say`) |
| pause/resume/stop | `/mission_fsm/request_pause`, `/request_resume`, `/request_stop` |
| return/base/redirect | `/mission_fsm/go_home`, `/go_to_base`, `/redirect` |
| interaction confirm/question/answer | `/mission_fsm/submit_confirm`, `/request_question`, `/submit_answer` |
| localization/reset | `/mission_fsm/reset_localization` (`ResetLocalization`) |
| face override/clear | `/face/override` (`OverrideFace`) |
| costmaps/clear | `/global_costmap/clear_entirely_global_costmap` и `/local_costmap/clear_entirely_local_costmap` |

В таблице сокращённые имена в одной строке сохраняют префикс `/mission_fsm`.
Часть адресов задаётся параметрами
[api.yaml](../guide_robot_api/config/api.yaml), часть фиксирована в коде.

Источники: [http_server.py](../guide_robot_api/guide_robot_api/lib/http_server.py),
[command_adapter.py](../guide_robot_api/guide_robot_api/command_adapter.py),
[operations.py](../guide_robot_api/guide_robot_api/lib/operations.py),
[api_node.py](../guide_robot_api/guide_robot_api/api_node.py).

## 17. HTTP и WebSocket лица на 8090

Это отдельный сервер, с полными путями относительно его собственного адреса:

- GET `/` — HTML рендера;
- GET `/states.json` — полная конфигурация состояний и темы;
- GET `/ws` — кадры `{state,gaze_az,seq}`, включая последний известный кадр
  сразу после подключения, если он есть;
- GET `/{path}` — файлы из web-каталога, без листинга каталога.

В этом сервере нет middleware авторизации host API, публичного конверта ошибок
или обработчика изменения выражения через HTTP. Выражение меняется через
операторский `/api/v1/face/override` и ROS-агрегатор. `source` есть в ROS
`FaceState` и `State.robot.face`, но отдельный WS рендера его не передаёт.

Источник: [face_server.py](../guide_robot_face/guide_robot_face/face_server.py).

## 18. Ошибки и таймауты публичного API

Обычная JSON-ошибка имеет форму:

```json
{"error":{"code":"invalid_state","message":"<причина>","request_id":"<UUID>"}}
```

При некоторых сервисных отказах рядом присутствует `state`. Middleware
сохраняет это поле, но не сохраняет произвольные остальные поля исходного
ответа, например `retry_after_s`, `costmaps_cleared` или `effective_state`.

| HTTP | `error.code` |
|---|---|
| 400 | `invalid_arguments` |
| 401 | `auth_required` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `invalid_state`; при точном `message="localization_required"` — `localization_required` |
| 429 | `rate_limited` |
| 500 | `internal_error` |
| 502, 503 | `subsystem_unavailable` |
| 504 | `outcome_unknown` |

`message` сохраняет конкретную причину или текст ROS, поэтому не обязательно
является стабильным англоязычным enum. Например, `idempotency_conflict` находится
в `message`, а код для него — `invalid_state`. Для HTTP-статусов, отсутствующих
в таблице, middleware использует исходную причину; универсального закрытого
enum для всех ошибок нет.

Основные таймауты исходников:

| Ожидание | Значение |
|---|---|
| Проверка bridge `/healthz` хостом | 1 с |
| Host HTTP JSON-запрос к bridge | 15 с |
| ROS-сервис/принятие action в bridge | `service_timeout_s`, default 5 с; постановка в executor и ожидание future — отдельные этапы |
| Файловое соединение к bridge / чтение | connect 3 с, sock_read 30 с, без общего total |
| Запрос к системному Unix helper | 150 с |
| Запуск helper питания | 10 с |

Сетевая ошибка до подключения командного bridge даёт `503`; ошибка во время
командного запроса/таймаут — `504`. Ошибка GET JSON-каталога через bridge
возвращается как `502 bridge_unreachable`. `504` не доказывает, что команда
не была исполнена. Для операций следует читать запись с прежним ID/ключом;
для сервисов — наблюдать состояние, учитывая отсутствие автоматической
идемпотентности этих маршрутов.

Источник: [http_policy.py](../scripts/jetson-launcher/guide_launcher/http_policy.py),
[bridge.py](../scripts/jetson-launcher/guide_launcher/bridge.py),
`_call_ros` в [api_node.py](../guide_robot_api/guide_robot_api/api_node.py).

## 19. Уточнения к схеме и границы проверки

При интеграции существенны следующие детали именно реализации:

- OpenAPI у `/public/start-tour` ещё описывает тело и `Idempotency-Key`,
  но обработчик их не читает и не запускает миссию.
- Для ряда сервисных POST схема требует тело, тогда как `command` допускает
  его отсутствие как `{}`; `/face/clear`, stack start/restart и cancel тела
  вообще не читают. Авторизация использует свой разбор JSON, без общего
  запрета неизвестных полей.
- Для `/localization/reset` код дополнительно запрещает одновременно pose
  и preset, требует map_version с pose и проверяет пределы координат.
  Один `200` не означает `costmaps_cleared=true` или `localization.ready`.
- Тип `hidden:boolean` в описании Wi-Fi строже исполняемой проверки:
  helper применяет `bool`, как описано в разделе 13.
- Сырые `MissionState` содержат больше полей, чем кадр API. Например,
  `resume_token`, `presence`, `detail`, `next_stop_id` не копируются в `robot`
  функцией `build_frame`; не следует ожидать их только потому, что они есть
  в ROS-сообщении.
- В `api_node.py` сохранён старый метод `_on_api_localization_reset`, однако
  активный dispatcher использует `ResetLocalization` через `PoseControl`.
  Старый метод не определяет поведение нынешнего endpoint.
- Нет публичных маршрутов загрузки/редактирования контента, изменения карты,
  произвольного ROS service/topic, управления `/cmd_vel`, stream-TTS или
  отдельного снятия supervisor estop. Перечисленные выше HTTP-команды не
  подразумевают таких возможностей.

Список маршрутов сверен с реально собранным router приложения без запуска
фонового управления. Проверки хостовых auth/session/API v1, регрессий,
настроек и питания на тестовых зависимостях: **96 passed, 7 skipped**.
Семь тестов Wi-Fi helper пропущены из-за отсутствующего `dbus_next`;
поведение helper в этом документе сверено чтением его кода. ROS-адаптер,
FSM и типы сообщений сверены по исходникам. Это не результаты живого
аппаратного прогона; ROS, Docker, сеть робота и питание в рамках подготовки
документа не переключались.

Использованные тесты:
[test_api.py](../scripts/jetson-launcher/tests/test_api.py),
[test_v1.py](../scripts/jetson-launcher/tests/test_v1.py),
[test_review_regressions.py](../scripts/jetson-launcher/tests/test_review_regressions.py),
[test_auth.py](../scripts/jetson-launcher/tests/test_auth.py),
[test_session.py](../scripts/jetson-launcher/tests/test_session.py),
[test_system_settings.py](../scripts/jetson-launcher/tests/test_system_settings.py),
[test_system_helpers.py](../scripts/jetson-launcher/tests/test_system_helpers.py),
[test_power.py](../scripts/jetson-launcher/tests/test_power.py).
