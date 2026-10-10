# guide-launcher

Хостовый сервис Jetson (systemd, `User=jetson`, порт `127.0.0.1:8089`). Он держит большой экран
робота живым **независимо от ROS-стека** и отдаёт две вещи:

1. **Экран показа** без входа: во время тура слайды экспоната, вне тура — промо. Внизу маленькая
   кнопка «Начать экскурсию» (только текущий тур, с подтверждением).
2. **Меню оператора** (4 нажатия за 2 с в правый верхний угол → PIN или RFID): выбор текущего
   тура, старт/стоп/домой, сброс локализации, очистка костмапов, статус и запуск/перезапуск
   ROS 2.

`operator_ui_node` в контейнере — теперь только мост ROS ↔ HTTP на `:8091` без своей страницы
(`guide_robot_operator_ui/README.md`). Лицо (`guide_robot_face`, `:8090`, `kiosk-face`) не
затрагивается.

```
kiosk-hdmi (WebKit) → http://127.0.0.1:8089

guide-launcher (хост, systemd)                              operator_ui_node (контейнер, :8091)
├─ GET  /, /static/*            страница                    ├─ GET  /ws            кадры состояния
├─ GET  /api/config, /api/promo, /promo/*   промо с хоста   ├─ GET  /api/tours
├─ /api/auth/*                  PIN / RFID, cookie          ├─ GET  /api/media/{id}, /media/*
├─ /api/stack/*                 состояние, start, restart   └─ POST /api/tour/start|stop, /api/go_home,
├─ /api/tour/current            текущий тур (state.json)           /api/localization/reset,
├─ /api/op/*     [вход]         команды роботу ──────────────────→ /api/costmaps/clear
├─ /api/public/start_tour       без входа, только текущий тур      (X-Bridge-Token + X-Operator)
└─ /ros/*  GET-прокси на :8091 (включая WS /ros/ws)
```

Стек запускается **только кнопкой** (`autostart_stack: false` по умолчанию). Автоперезапуска
нет.

## Установка

С хоста Jetson, из чекаута репозитория (`~/Desktop/Projects/Robo-guide`):

```bash
sudo scripts/jetson-launcher/install.sh            # проверки + установка
scripts/jetson-launcher/install.sh --preflight-only  # только проверки, ничего не менять
DRY_RUN=1 scripts/jetson-launcher/install.sh         # печатает действия установки
```

Скрипт идемпотентен. **Preflight ничего не угадывает**: любая расходимость печатает
`[FAIL]` и завершает скрипт с кодом 1 до первого изменения (единственное исключение —
`apt-get install python3-aiohttp python3-yaml python3-serial`, они нужны самому preflight;
pip в системный Python не используется).

| Проверка preflight | Если `[FAIL]` |
|---|---|
| хост импортирует `aiohttp`, `yaml`, `serial` | `install.sh` доставит пакеты через apt (в режимах `--preflight-only`/`DRY_RUN` только сообщит) |
| контейнер `robo-guide-jetson-1` запущен, `NetworkMode=host`, репозиторий смонтирован в `/home/fabian/ros2_ws/src`, `/dev` смонтирован | контейнер создан не из `compose.yaml`; `docker compose up -d jetson` |
| `Config.Cmd` контейнера равен `command` из `compose.yaml` | контейнер создан до правки compose. **Сначала остановить стек из меню**, затем `docker compose up -d --force-recreate jetson` (пересоздание убивает процессы контейнера через SIGKILL, см. «Безопасность перезапуска») |
| `jetson` в группе `docker` | `sudo usermod -aG docker jetson`, перелогиниться; скрипт сам этого не делает |
| порт 8089 свободен или занят самим `guide-launcher` | другой процесс на 8089: `ss -ltnp \| grep 8089` |
| существуют `/etc/guide-kiosk/url` и `kiosk-hdmi.service` | киоск на роботе настроен иначе, чем описано в `scripts/jetson-display/README.md` |
| `/usr/local/bin/guide-kiosk-hdmi` совпадает с копией в репозитории или с любой прошлой ревизией из git | на роботе правили руками: печатается diff, разберитесь и решите, что верно |
| `guide-kiosk-face` читает `/etc/guide-kiosk/url` (печатается `grep -n url`) | лицо берёт адрес откуда-то ещё; ничего не менять, разобраться вручную |
| в контейнере не заданы `ROS_DOMAIN_ID`, `RMW_IMPLEMENTATION`, `CYCLONEDDS_URI`, `ROS_LOCALHOST_ONLY`, `FASTRTPS_DEFAULT_PROFILES_FILE` | `start_stack.sh` их не выставляет; стек, поднятый кнопкой, не увидел бы ноды, запущенные руками |
| UID:GID `jetson` и `fabian` в контейнере | не FAIL: если равны, токен `0640`, иначе `0644` |

Что делает установка (после успешного preflight):

- `guide_launcher/` и `web/` → `/opt/guide-launcher`;
- `/etc/guide-launcher/config.yaml` из `config.example.yaml`, **только если его нет**
  (путь чекаута подставляется автоматически); проверьте `operator_pin`, `default_tour`,
  `rfid_secret_file`;
- `<репозиторий>/.guide_launcher/{COLCON_IGNORE,bridge_token}`; токен генерируется, только если
  его нет, существующий не перезаписывается (после `git clean -fdx` он просто создастся заново,
  для ноды это обычный рестарт стека). Каталог в `.gitignore`;
- `guide-launcher.service`, drop-in `kiosk-hdmi.service.d/10-guide-launcher.conf`
  (`After=`/`Wants=` launcher), `guide-kiosk-hdmi` в `/usr/local/bin`;
- `/etc/guide-kiosk/url-hdmi` = `http://127.0.0.1:8089` (адрес большого экрана),
  `/etc/guide-kiosk/url` = `http://127.0.0.1:8090` (лицо: его читает `guide-kiosk-face`);
- `daemon-reload`, `enable --now` и `restart` для `guide-launcher`, `restart kiosk-hdmi`.

`systemctl restart guide-launcher` **не трогает стек**: launcher при старте только опрашивает
состояние.

### Токен моста: два пути к одному файлу

| Кто | Путь |
|---|---|
| launcher (хост), `bridge_token_file` | `~/Desktop/Projects/Robo-guide/.guide_launcher/bridge_token` |
| `operator_ui_node` (контейнер), параметр `bridge_token_file` | `/home/fabian/ros2_ws/src/.guide_launcher/bridge_token` |

Один и тот же файл через bind-mount репозитория. Контейнер пересоздавать не нужно.

## Конфиг `/etc/guide-launcher/config.yaml`

Образец — `config.example.yaml`.

| Ключ | По умолчанию | Смысл |
|---|---|---|
| `bind_host` / `http_port` | `127.0.0.1` / `8089` | адрес страницы |
| `container` | `robo-guide-jetson-1` | `""` → управление стеком выключено (разработка на ноутбуке) |
| `start_cmd` | `/home/fabian/ros2_ws/src/scripts/jetson-launcher/start_stack.sh` | путь **внутри контейнера** |
| `stack_log` | `/tmp/stack.log` | лог стека внутри контейнера, перезаписывается при старте |
| `bridge_url` / `bridge_token_file` | `http://127.0.0.1:8091` / см. выше | мост в ноду |
| `autostart_stack` | `false` | запустить стек один раз после первого опроса, если он `DOWN` |
| `start_timeout_s` | `120` | сколько ждать мост после команды |
| `poll_interval_s` | `2.0` | период опроса состояния |
| `promo_dir` | `<репозиторий>/guide_robot_operator_ui/promo` | `promo.yaml` и `media/` |
| `promo_interval_s` / `slide_interval_s` | `10.0` / `8.0` | длительность кадра, если у элемента нет своей |
| `always_promo` | `true` | `true` — промо и во время тура; `false` — во время тура слайды экспоната |
| `default_tour` | `expo_one` | текущий тур, пока в `state.json` ничего не выбрано |
| `operator_pin` | — | не короче 8 символов, иначе сервис не стартует (exit 2) |
| `auth_backends` | `[rfid, pin]` | `pin` обязателен; `rfid` без порта/секрета просто недоступен |
| `rfid_port` / `rfid_secret_file` | `/dev/rfid0` / `""` | `/dev/rfid0` всегда принадлежит launcher'у; пустой секрет — вход только по PIN |
| `session_ttl_s` | `600` | скользящее окно сессии |
| `state_dir` | `~/.guide_robot/launcher` | `state.json` и журнал |

## Состояние стека

Опрос раз в `poll_interval_s`, три проверки:

| Проверка | Как |
|---|---|
| `container` | `docker inspect -f '{{.State.Running}}' <container>` |
| `launch` | `docker exec <container> pgrep -f "ros2 launch"` |
| `bridge` | `GET :8091/api/tours` → 200, таймаут 1 с |

| `state` | Когда |
|---|---|
| `UP` | все три проверки ок |
| `DOWN` | контейнер или `launch` мёртвы, команды не в полёте |
| `STARTING` | команда отправлена, ждём мост (до `start_timeout_s`); либо контейнер и launch живы, а мост не отвечает (тоже до `start_timeout_s`: это покрывает запуск руками и рестарт launcher'а) |
| `DEGRADED` | контейнер и launch живы, мост молчит дольше `start_timeout_s` |
| `FAILED` | истёк таймаут `STARTING` или команда docker вернула ошибку; держится до `UP` или новой команды |

Переход `UP` → не-`UP` по мосту происходит только после **3 неудачных проверок подряд**
(гистерезис против мерцания на загруженном Jetson). Смерть контейнера или `launch` даёт `DOWN`
сразу. `start` и `restart` сериализуются; повтор во время `STARTING` → 409 `starting`. При
`container: ""` проверки контейнера считаются пройденными, состояние определяет только мост, а
раздел «ROS 2» в меню скрыт.

- **start**: `docker start` (если контейнер не запущен), затем
  `docker exec -d -e STACK_LOG=<stack_log> <container> <start_cmd>`. `start_stack.sh`
  идемпотентен (если `ros2 launch` жив — выходит с 0), сам делает `source` ROS (`docker exec` не
  читает `.bashrc`), пишет stdout+stderr в `stack_log` и запускает
  `ros2 launch guide_robot_bringup robot.launch.py` через `exec`.
- **restart**: см. следующий раздел.

Живость моста: launcher держит WS-клиент на `:8091/ws`. Нода шлёт кадр по каждому сообщению
ROS **и heartbeat-кадром раз в 1 с**, поэтому кадр старше 3 с считается отсутствующим (зависший
мост, не закрывший сокет, не выглядит «свежим»). Слайды и промо при мёртвом ROS никогда не
показывают ошибок: просто крутится промо.

### Безопасность перезапуска

Драйвер колёс FURO **не имеет собственного таймаута и держит последнюю принятую скорость** до
следующего пакета (`guide_robot_hardware/src/guide_robot_system.cpp:434`, `:454`). Стоп-пакет
шлют `on_deactivate` (`:447`), `on_error` (`:476`), деструктор (`:197`, «последний рубеж на
случай SIGINT») и watchdog `cmd_timeout = 0.3 с` (`:1057`), но watchdog — поток внутри процесса
`ros2_control_node` и умирает вместе с ним. **`SIGKILL` стоп-пакета не отправляет: база едет с
последней скоростью.**

PID 1 контейнера — интерактивный `bash` из `command` в `compose.yaml`; он игнорирует SIGTERM,
поэтому голый `docker restart -t 20` всегда ждёт весь таймаут и затем убивает весь
pid-namespace SIGKILL'ом. Поэтому `restart` делает по шагам:

1. `docker exec <c> pkill -INT -f "ros2 launch"`: launch штатно гасит дочерние процессы
   (SIGINT → SIGTERM → SIGKILL со своими таймаутами), деструктор драйвера шлёт стоп;
2. ждёт до 20 с, пока `pgrep -f "ros2 launch"` не станет пустым;
3. `docker restart -t 20 <container>`;
4. `docker exec -d … start_cmd`.

Если за 20 с launch не завершился, в журнал launcher'а пишется предупреждение, и `docker restart`
всё равно выполняется.

Дополнительно `/api/stack/restart` возвращает **409 `robot_moving`**, если по последнему
свежему кадру идёт тур (`state_name` не `idle`/`unknown` при живой связи с `mission_fsm`,
`mission_state_age_s ≤ 3`). Оператор сначала жмёт «Стоп». Если кадра нет или связь с
`mission_fsm` потеряна (`DOWN`/`FAILED`/`DEGRADED`), рестарт разрешён: иначе мёртвый
`mission_fsm` нельзя было бы вылечить. По той же причине `docker compose up --force-recreate`
делайте только после остановки стека.

## Вход

- 4 нажатия за 2 с в правый верхний угол (угол 4×4 rem; окно скользящее, нажатия ближе
  60 мс считаются дребезгом контакта) → окно входа (ожидание RFID и PIN-клавиатура).
- PIN сравнивается на сервере (`hmac.compare_digest`). RFID — HMAC challenge-response с ридером
  на `rfid_port`, секрет из `rfid_secret_file`; протокол и ограничения (MIFARE Classic
  клонируется) — `guide_robot_operator_ui/firmware/RFID_README.md`. Порт открывается лениво и
  переоткрывается, если ридер был не подключён при старте.
- Сессия — cookie `gl_session` (`HttpOnly`, `SameSite=Strict`, `Path=/`, без `Secure`: киоск по
  http на 127.0.0.1). Одна активная сессия; новый вход вытесняет прошлый. Скользящее окно
  `session_ttl_s`, продлевается **только успешными POST**.
- Nonce живёт 30 с и расходуется на любую попытку. Локаут: **10 неудач за 60 с** →
  `429 locked_out` с `retry_after_s`.
- Безопасность рассчитана на посетителя музея; потолок стойкости — PIN.

## Текущий тур и публичный старт

Текущий тур хранится в `state_dir/state.json` (атомарная запись) и переживает и рестарт ROS, и
рестарт launcher'а. Пока не выбран — `default_tour`. `GET /api/tour/current` открыт,
`POST /api/tour/current {tour_id}` требует входа.

`POST /api/public/start_tour` — **единственная команда без входа**. Без тела; `tour_id` от
клиента игнорируется, стартует всегда текущий тур с `X-Operator: public`. Стопа с экрана
показа нет. Отказ — `409 {"error":"refused","reason":…}`; проверки по порядку:

| `reason` | Условие |
|---|---|
| `rate_limited` | прошло меньше 10 с с прошлой попытки |
| `ros_down` | `state != UP` (или мост не отдал `/api/tours`) |
| `no_mission_fsm` | нет свежего кадра или `mission_state_age_s` пуст/больше 3 |
| `estop` | `estop` в кадре или `supervisor_state` FAULT/SHUTDOWN |
| `tour_active` | `state_name` не `idle`/`unknown` |
| `unknown_tour` | текущего тура нет в `/api/tours` |
| `upstream_<код>` | мост ответил ошибкой (5xx → HTTP 502) |

Кнопка на экране видна при тех же условиях; название берётся из `/ros/api/tours`, если id тура
там нет — кнопка скрыта.

## API launcher'а

| Путь | Вход | Назначение |
|---|---|---|
| `GET /`, `/static/*` | нет | страница |
| `GET /api/config` | нет | `always_promo`, интервалы, `stack_control` |
| `GET /api/promo`, `/promo/*` | нет | манифест (с полем `rev`) и файлы промо |
| `GET /api/stack/status` | нет | `{state, checks:{container,launch,bridge}, control, last_error}` |
| `POST /api/stack/start`, `/restart` | да | 202 `{ok,state}`; 409 `starting`/`robot_moving`; 400 `stack_control_disabled` |
| `GET /api/stack/log` | да | `{lines}`: последние 30 строк `stack_log` |
| `POST /api/auth/challenge`, `/verify`, `/logout`; `GET /api/auth/status` | нет | вход |
| `GET /api/tour/current` / `POST /api/tour/current` | нет / да | текущий тур |
| `POST /api/op/tour/start`, `/tour/stop`, `/go_home`, `/localization/reset`, `/costmaps/clear` | да | команды в мост; 409 `ros_down`, если не `UP` |
| `POST /api/public/start_tour` | нет | см. выше |
| `GET /ros/*`, WS `/ros/ws` | нет | прокси на мост, allowlist: `api/tours`, `api/media/*`, `media/*`, `ws`; не-GET → 405 |

Без сессии — `401 {"error":"auth_required"}`. Команды оператора всегда стартуют текущий тур,
тело клиента игнорируется.

## Промо

`promo.yaml` и `media/` читаются прямо из исходников (`promo_dir`), **без сборки и рестартов**:
launcher перечитывает манифест при смене mtime `promo.yaml`, а страница перезапрашивает его на
каждом обороте цикла и перезапускает показ, если изменилось поле `rev`. Добавить картинку:
положить файл в `guide_robot_operator_ui/promo/media/`, вписать в `promo.yaml`, через один
оборот цикла она на экране. Промо отдаётся с хоста, поэтому крутится и при мёртвом ROS.

## Журнал

`state_dir/launcher_YYYYmmdd_HHMMSS.jsonl` (`~/.guide_robot/launcher/`), одна строка на событие
с результатом: `auth_attempt`, `logout`, `command`, `public_start`, `stack`, `tour_current`.

## Диагностика

Порядок от службы к железу:

```bash
systemctl status guide-launcher
journalctl -u guide-launcher -b
curl -s 127.0.0.1:8089/api/stack/status          # state, три проверки, last_error
# причина, почему стек не поднялся: меню → ROS 2 → лог, или напрямую
docker exec robo-guide-jetson-1 tail -n 50 /tmp/stack.log
docker exec robo-guide-jetson-1 pgrep -af "ros2 launch"
curl -s 127.0.0.1:8091/api/tours                 # мост напрямую
curl -s -X POST 127.0.0.1:8091/api/tour/start    # без токена → 403 (так и задумано)
```

| Симптом | Причина |
|---|---|
| `DOWN`, кнопка не появляется, крутится промо | стек не запущен: меню → ROS 2 → «Запустить» |
| `STARTING` дольше 2 минут → `FAILED` | смотреть лог стека (`start_stack.sh` пишет причину: нет `install/setup.bash` → `colcon build`) |
| `DEGRADED` | launch жив, а мост молчит: нода `operator_ui` упала (нет токена? `grep operator_ui /tmp/stack.log`) |
| сервис не стартует | `journalctl -u guide-launcher`: PIN короче 8 символов или пустой файл токена (exit 2) |
| «Сначала нажмите Стоп» | `restart` во время тура (`robot_moving`) |

Про сами экраны (weston, донгл, белый экран) — `docs/kiosk_operator_ui_explained.md` и
`scripts/jetson-display/README.md`.

## Разработка на ноутбуке

`container: ""` в конфиге: управление стеком выключено, раздел «ROS 2» скрыт, состояние
определяет только мост.

```bash
mkdir -p ~/.guide_launcher && head -c32 /dev/urandom | base64 | tr -d '=+/\n' > ~/.guide_launcher/bridge_token
cp scripts/jetson-launcher/config.example.yaml /tmp/launcher.yaml   # container: "", bridge_token_file, promo_dir
cd scripts/jetson-launcher && python3 -m guide_launcher --config /tmp/launcher.yaml
python3 -m pytest scripts/jetson-launcher/tests -q                 # из корня репозитория
```

Нода для полной проверки: `operator_ui.launch.py params_file:=…/config_dev/operator_ui_dev.yaml`
с тем же файлом токена.

## Приёмка на Jetson

Проверяется на роботе после `sudo scripts/jetson-launcher/install.sh`:

1. Холодная загрузка: промо на экране, кнопки «Начать экскурсию» нет; в меню (после входа) раздел
   ROS 2 показывает `DOWN`, «Экскурсия» и «Робот» неактивны.
2. «Запустить» → `STARTING` → `UP` без ручных действий; появляется кнопка с названием текущего
   тура.
3. Публичный старт: подтверждение запускает тур; «Отмена» и 15 с бездействия закрывают диалог;
   во время тура кнопки нет, показываются слайды (`always_promo: false`) или промо.
4. `curl -X POST 127.0.0.1:8091/api/tour/start` без токена → 403.
5. `docker exec robo-guide-jetson-1 pkill -f "ros2 launch"` → промо продолжает крутиться, кнопка
   пропадает, в меню `DOWN` и лог.
6. «Перезапустить» → стек поднят, страница сама переподключилась, сессия оператора сохранилась.
7. Вход по RFID работает и при `UP`, и при `DOWN`.
8. Смена текущего тура в меню → кнопка меняет название; выбор сохраняется после перезапуска
   стека и после `systemctl restart guide-launcher`.
9. Новый файл в `promo.yaml` (без сборки) появляется на экране в течение одного оборота цикла.
10. `systemctl restart guide-launcher` при живом стеке не трогает стек.
11. После «Перезапустить» глаза на лицевой панели возвращаются без ручных действий
    (`face_node` живёт в `high_level_stack` и падает вместе со стеком; страница лица
    переподключает WS с бэкоффом 1→10 с, `guide_robot_face/web/face.js:193`).
