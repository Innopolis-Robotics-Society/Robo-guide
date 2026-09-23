# Киоск, показ картинок и интерфейс оператора — как это работает целиком

Документ описывает цепочку от физического экрана до ROS-сервисов: что запускает браузер на
Jetson, откуда берётся картинка на большом экране, что показывает страница оператора и в
каких случаях экран остаётся пустым.

Соседние документы: `scripts/jetson-display/README.md` (донгл FL2000, weston, тач, режимы
экрана, известные аппаратные проблемы), `guide_robot_operator_ui/README.md` (детали
протокола панели), `guide_robot_face/README.md` (лицо робота).

---

## 1. Общая схема

```
  ROS-стек (docker robo-guide-jetson-1)
  ├─ operator_ui_node ──── aiohttp :8091 ──┐        ┌── kiosk-hdmi.service (WebKit GTK, Wayland)
  │   /mission/state, RunTour, промо/медиа │        │      └─ weston-dlhdmi.service → донгл FL2000 → большой экран
  │                                        ├────────┤
  └─ face_node ─────────── aiohttp :8090 ──┘        └── kiosk-face.service (firefox --kiosk, GNOME/X) → панель DP-1
```

Два независимых экрана и два независимых HTTP-сервера. Оба сервера — часть ROS-нод, поэтому
без запущенного ROS-стека браузеры открывать нечего.

| Что | Экран | Служба на Jetson | Адрес |
|---|---|---|---|
| Промо-картинки и панель оператора | большой экран через донгл FL2000 | `weston-dlhdmi.service` + `kiosk-hdmi.service` | `http://127.0.0.1:8091` |
| Лицо робота (два SVG-глаза) | встроенная панель DP-1 | `kiosk-face.service` | `http://127.0.0.1:8090` |

---

## 2. Сторона Jetson: как запускается киоск

Файлы киоска живут вне ROS-workspace, копии части из них — в `scripts/jetson-display/`.

| Роль | Где на Jetson |
|---|---|
| Компоситор на донгле | `weston-dlhdmi.service`, `/etc/xdg/weston-dlhdmi/weston.ini` |
| Браузер большого экрана | `kiosk-hdmi.service` → `/usr/local/bin/guide-kiosk-hdmi` (копия: `scripts/jetson-display/guide-kiosk-hdmi`) |
| Браузер лицевой панели | `kiosk-face.service` → `/usr/local/bin/guide-kiosk-face` |
| Адрес страницы | `/etc/guide-kiosk/url` |

`kiosk-hdmi.service` объявляет `Requires=weston-dlhdmi.service` и `After=` — без компоситора
браузер не стартует. Работает под пользователем `jetson` с `Nice=15`, `CPUWeight=20`,
`WEBKIT_DISABLE_COMPOSITING_MODE=1`, `LIBGL_ALWAYS_SOFTWARE=1` (программная отрисовка),
`Restart=always`.

### 2.1 Порядок запуска — главный источник проблем

Киоски стартуют вместе с системой, а ROS-стек поднимается **вручную и позже**
(`operator_ui_node` не имеет systemd-юнита, см. раздел 9). Поэтому в `guide-kiosk-hdmi`
есть своя логика повторов:

- при `load-failed` главного документа скрипт пишет в журнал `не загрузился <url>: ...;
  повтор через 3 с` и перезагружает страницу каждые `RETRY_S = 3` с;
- при гибели web-процесса WebKit (`web-process-terminated`) — то же самое;
- стандартная страница ошибки WebKit подавляется.

Чего эта логика **не** делает: если страница один раз успешно загрузилась, а ROS-стек потом
перезапустили, браузер страницу не перезагружает. Страница сама тоже не перезапрашивает ни
список промо, ни список туров (раздел 5.3) — она лишь переподключает WebSocket. Отсюда
правило эксплуатации:

```bash
# после каждого перезапуска ROS-стека
sudo systemctl restart kiosk-hdmi
```

### 2.2 Замечание по адресу страницы (проверено 20.09.2026)

`/etc/guide-kiosk/url` содержит `http://127.0.0.1:8091`, и `guide-kiosk-face` читает **тот же
файл**. То есть обе службы открывают страницу оператора, а лицо (`:8090`) ни одна служба не
открывает. В `scripts/jetson-display/README.md` записано иначе («kiosk-face ждёт
`http://127.0.0.1:8090`»), так что либо скрипт лицевой панели меняли, либо адрес на панели
задавали вручную. Перед следующим выездом это стоит проверить на месте:
`cat /etc/guide-kiosk/url`, `grep url /usr/local/bin/guide-kiosk-face`.

---

## 3. Сторона ROS: нода operator_ui_node

Запускается отдельно от `hardware.launch.py`:

```bash
docker exec -d robo-guide-jetson-1 bash -c \
  "cd /home/fabian/ros2_ws && source install/setup.bash && \
   exec ros2 launch guide_robot_operator_ui operator_ui.launch.py > /tmp/operator_ui.log 2>&1"
```

Параметры — `guide_robot_operator_ui/config/operator_ui.yaml`, объявления в
`guide_robot_operator_ui/guide_robot_operator_ui/operator_ui_node.py:92-149`.

Основные:

| Параметр | Дефолт | Смысл |
|---|---|---|
| `bind_host` / `http_port` | `127.0.0.1` / `8091` | слушает только сам робот; 8090 занят лицом |
| `web_root` | `<share>/web` | `index.html`, `app.js`, `app.css` |
| `promo_dir` | `<share>/promo` | `promo.yaml`; картинки — в его подкаталоге `media/` |
| `media_root` | `<share guide_robot_semantic_map>/content/media` | медиа экспонатов |
| `promo_interval_s` / `slide_interval_s` | `10.0` / `8.0` | длительность кадра, если у элемента нет своей |
| `service_timeout_s` | `5.0` | единственный источник HTTP 503 на ROS-вызовах |
| `content_language` / `tours_language` | `ru` / `ru` | языки контента и списка туров |
| `operator_pin` | `changeme` | короче 8 символов — нода не стартует; в боевом конфиге `00112233` |
| `auth_backends` | `["rfid","pin"]` | `mock` отключает проверку и вешает плашку в интерфейсе |
| `session_ttl_s` | `600.0` | скользящее окно сессии оператора |
| `rfid_port` / `rfid_secret_file` | `/dev/rfid0` / `""` | пустой секрет — вход только по PIN |

Имена всех ROS-сервисов и экшена тоже параметры (`run_tour_action`,
`request_stop_service`, `go_home_service`, `list_tours_service`, `list_locations_service`,
`get_exhibit_content_service`, `get_exhibit_media_service`, сервисы очистки костмапов).
Жёстко зашиты только подписки `/mission/state`, `/supervisor/estop`, `/supervisor/state` и
публикация `/initialpose` (`operator_ui_node.py:267-307`).

### 3.1 HTTP-роуты

Регистрация — `guide_robot_operator_ui/guide_robot_operator_ui/lib/ui_server.py:112-143`.

| Путь | Что отдаёт | Нужен вход |
|---|---|---|
| `GET /` | `index.html` | нет |
| `GET /static/*` | `app.js`, `app.css` | нет |
| `GET /ws` | WebSocket, кадры состояния сервер → клиент | нет |
| `GET /api/promo` | манифест промо | нет |
| `GET /promo/*` | файлы промо из `promo_dir/media` | нет |
| `GET /api/media/{exhibit_id}` | манифест слайдов экспоната | нет |
| `GET /media/*` | файлы медиа из `media_root` | нет |
| `GET /api/tours` | список туров, интервал слайдов, настройки входа | нет |
| `GET/POST /api/auth/*` | challenge, verify, status, logout | нет |
| `POST /api/tour/start`, `/api/tour/stop`, `/api/go_home`, `/api/localization/reset` | команды роботу | **да** |

Вход требуется ровно для четырёх командных запросов (`ui_server.py:36-38`, проверка в
middleware `_auth_gate`, `ui_server.py:196-218`). Всё, что нужно для показа картинок,
доступно без входа — киоск работает как есть.

На `/promo/`, `/media/`, `/static/` добавляется `Cache-Control: no-cache`
(`ui_server.py:230-234`): без него WebKit киоска считал подменённые картинки свежими и
показывал старые. На сам `GET /` заголовок не ставится, поэтому правка `index.html` может не
подхватиться до чистки кэша браузера.

---

## 4. Страница: три слоя

`guide_robot_operator_ui/web/index.html`, логика — `web/app.js`.

1. **Медиа-слой** (`#media-layer`) — смонтирован всегда, чёрный фон, курсор скрыт. Внутри:
   заставка `#idle-screen` с текстом «Экскурсовод», два слоя кроссфейда `#slide-layer-a/b`,
   титульная карточка `#title-card`, экран «Еду на базу» `#returning-screen` и невидимый
   уголок `#unlock-corner` в правом верхнем углу.
2. **Панель оператора** (`#panel-overlay`, `position: fixed`, поверх медиа) — статус, выбор
   тура, кнопки «стоп», «домой», «сброс локализации», управление сессией. Её открытие и
   закрытие не трогает медиа-слой.
3. **Плашки** (`#badges`) — «E-STOP», «Нет связи с mission_fsm», «Аутентификация отключена».
   Не перехватывают касания.

Важное: фон страницы и заставки — чёрный (`app.css:58-75`). **Белый экран страница дать не
может**; белое — это пустой WebKit или отсутствие окна на компоситоре, а не эта страница.

### 4.1 Что выбирает содержимое медиа-слоя

`updateMediaLayer()` (`app.js:543-593`):

- сейчас в коде стоит `ALWAYS_PROMO = true` (`app.js:20-25`) — **промо крутится всегда**, и в
  простое, и во время тура. Слайды экспонатов, титульная карточка и экран «Еду на базу» при
  этом не показываются никогда;
- при выключенном `ALWAYS_PROMO` выбор такой: тур не активен → промо; активен и связь с
  `mission_fsm` потеряна → замереть на последнем слайде; состояние `returning` → «Еду на
  базу»; иначе слайд по паре (`exhibit_id`, `chunk_index`).

Открытая панель оператора ставит видео на паузу (`app.js:617-641`) — единственное, что
сейчас влияет на промо помимо самого цикла.

---

## 5. Промо: путь картинки до экрана

### 5.1 Откуда берётся

```
guide_robot_operator_ui/promo/promo.yaml   ──┐
guide_robot_operator_ui/promo/media/*.JPG  ──┤ colcon build → install/.../share/guide_robot_operator_ui/promo/
                                             ↓
            operator_ui_node читает promo.yaml ОДИН РАЗ при старте (operator_ui_node.py:209)
                                             ↓
                    GET /api/promo → {"items":[{id,kind,path,duration_s,caption}], "promo_interval_s":10.0}
                                             ↓
              app.js loadPromo() — ОДИН РАЗ при загрузке страницы (app.js:868) → цикл показа
                                             ↓
                        GET /promo/<path> → файл из promo/media/
```

Формат `promo.yaml`: `items: [{id, kind: image|video, file, duration_s?, caption?}]`. Разбор
(`lib/promo_io.py`) никогда не падает: битый YAML, отсутствующий файл или неверный `kind` дают
предупреждение в лог, а не отказ ноды. Отсутствующий на диске файл всё равно попадает в
манифест — это заметно только на экране.

Текущее содержимое: активен один элемент `{id: p0, kind: image, file: IMG_VIBORY.JPG,
duration_s: 0.0}`, остальные закомментированы. `duration_s: 0.0` означает «взять
`promo_interval_s`», то есть та же картинка переклеивается раз в 10 с.

Масштабирование: `width/height: 100%` + `object-fit: contain` (`app.css:103-108`) — картинка
вписывается в экран целиком, пропорции сохраняются, поля чёрные.

### 5.2 Как поменять картинки

```bash
# 1. положить файл
cp NEW.JPG guide_robot_operator_ui/promo/media/
# 2. вписать его в guide_robot_operator_ui/promo/promo.yaml
# 3. пересобрать (файлы ставятся в install/share)
colcon build --packages-select guide_robot_operator_ui
# 4. перезапустить ноду operator_ui  (манифест читается только при старте)
# 5. перезапустить киоск             (страница запрашивает манифест только при загрузке)
sudo systemctl restart kiosk-hdmi
```

Пропуск шага 4 или 5 — самая частая причина «положил картинку, а на экране старое».

### 5.3 Что НЕ перезапрашивается

Страница делает `GET /api/promo` и `GET /api/tours` ровно один раз при загрузке. При обрыве
WebSocket она переподключает только сокет (`app.js:663-671`), манифесты не перечитывает.
Периодический запрос в коде один — `/api/auth/status` раз в 5 с, и только при активной
сессии.

---

## 6. Слайды экспонатов (сейчас отключены флагом)

Путь, который заработает при `ALWAYS_PROMO = false`:

`/mission/state` даёт `exhibit_id` и `chunk_index` → страница просит
`GET /api/media/<exhibit_id>` → нода зовёт два ROS-сервиса `content_server`:
`GetExhibitContent(mode="full")` за списком `chunk_id` и `GetExhibitMedia` за файлами
(`operator_ui_node.py:600-668`) → страница выбирает элементы с совпадающим `chunk_id`, при
отсутствии — общие для экспоната (`app.js:296-307`) → файлы отдаются по `/media/<path>`.

Если у экспоната медиа нет (как у точек `vybory_*`), показывается титульная карточка с
названием экспоната. Манифесты кэшируются и на ноде, и в странице без инвалидации.

---

## 7. Вход оператора

- Долгое нажатие 2 с в невидимый правый верхний угол (`#unlock-corner`) открывает окно входа.
- Способы — `auth_backends`: `rfid` (карта на `/dev/rfid0`, секрет в файле из
  `rfid_secret_file`), `pin` (экранная клавиатура, значение `operator_pin`), `mock` (проверка
  отключена, в интерфейсе несъёмная предупреждающая плашка).
- Сессия — скользящее окно `session_ttl_s` (по умолчанию 10 минут), продлевается только
  успешными запросами. Все команды и входы пишутся в jsonl-лог в `command_log_dir`
  (`~/.guide_robot/operator_ui`).
- Кнопка «Старт тура» отправляет `RunTour` с `greet/narrate/confirm_between_stops/return_home
  = True` (`operator_ui_node.py:507-513`). Для езды по одной точке это не подходит — там
  используется `mission_cli tour --locations ... --no-greet --no-confirm --no-return-home`.

---

## 8. Что делать, если на большом экране не то

Порядок проверки от страницы к железу.

```bash
# 1. сервер жив и страница отдаётся
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8091/
for u in /static/app.js /static/app.css /api/promo; do curl -s -o /dev/null -w "$u %{http_code}\n" http://127.0.0.1:8091$u; done

# 2. манифест промо и сам файл
curl -s http://127.0.0.1:8091/api/promo
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://127.0.0.1:8091/promo/IMG_VIBORY.JPG

# 3. службы экранов и их журнал
systemctl is-active weston-dlhdmi kiosk-hdmi kiosk-face
journalctl -u kiosk-hdmi -b | grep -v -iE "dbus|atspi|a11y|Deprecation|gdk_"

# 4. перезагрузить страницу киоска
sudo systemctl restart kiosk-hdmi
```

Трактовка:

| Симптом | Причина |
|---|---|
| В журнале `не загрузился ...: Connection refused` | ROS-стек ещё не поднят; после запуска стека браузер сам подхватит через 3 с |
| Страница и файлы отдаются, а на экране старое | страница загружена до перезапуска стека — перезапустить `kiosk-hdmi` |
| Текст «Экскурсовод» | манифест промо пустой или все файлы битые: проверить `promo.yaml`, наличие файлов, пересобрать и перезапустить ноду |
| Картинка одна и не меняется | так и задумано: в `promo.yaml` активен один элемент |
| **Чисто белый экран** | это не страница (её фон чёрный): пустой WebKit, упавший web-процесс или отсутствие окна на компоситоре. Смотреть журнал `kiosk-hdmi` и `weston-dlhdmi`, наличие процессов `WebKitWebProcess`, `/dev/dri/card1`, и передёргивался ли донгл (`dmesg | grep fl2000`) |
| Экран мигает при касании, пропадает сигнал | аппаратная часть донгла — `scripts/jetson-display/README.md` |

---

## 9. Известные особенности и грабли

- `operator_ui_node` не переживает перезагрузку Jetson: systemd-юнита нет, запуск руками.
  Киоск при этом стартует сам и ждёт сервер.
- Манифест промо читается один раз при старте ноды, страница запрашивает его один раз при
  загрузке — два места, которые надо перезапускать после смены картинок.
- Битые URL запоминаются страницей навсегда (до перезагрузки страницы): один раз не
  отдавшийся файл выпадает из цикла. Для видео туда же ведёт событие `stalled`, то есть
  медленная отдача может выкинуть исправный файл.
- Сообщения об ошибках выводятся внутри панели оператора, поэтому на киоске без входа их не
  видно.
- `ALWAYS_PROMO = true` — временная мера, пока нет реальных медиа экспонатов. Пока флаг
  включён, весь путь слайдов тура на странице не исполняется, хотя сервер его поддерживает.
- Заголовок `no-cache` не ставится на `GET /`, поэтому правка `index.html` может не
  подхватиться сразу.
- На Jetson часы могут уезжать (наблюдалось расхождение журнала и `date` почти на 12 часов) —
  фильтры `journalctl --since` тогда врут, надёжнее `-b`.
- Обе службы киоска читают один и тот же `/etc/guide-kiosk/url` (см. 2.2): чтобы развести
  экраны, нужен отдельный файл адреса для лицевой панели.
