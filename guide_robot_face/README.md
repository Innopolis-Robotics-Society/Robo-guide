# guide_robot_face

Два SVG-глаза на DP-панели робота. Пакет ничего не знает про тур, LLM
или рот — только `state` + `gaze_az`. `ament_python`, ROS 2 Humble.

Панель на Jetson: DP-1 **MHS MH-XGD**, **1024×768 @ 60 Hz**, 4:3,
физически вверх ногами (`rotate_deg: 180` в `config/face_states.yaml`).

## Топология

```
/supervisor/state ─┐
/voice/speaking   ─┤
/dialog/phase     ─┤
/mission/state    ─┼─► face_aggregator ─► /face/state ─┐
/mission/presence ─┤                                   ├─► face_node ─► aiohttp :8090
/vad              ─┤  /face/set_state (отладка) ───────┘
/speech/wakeword  ─┘
Firefox --kiosk (хост Jetson, не docker) ──http://127.0.0.1:8090──► DP-1
```

`face_node` не lifecycle. Выражения живут в YAML, JS только твинит
`w/h/rr/rot/curve/gx/gy` и рисует моргание / дыхание / саккады поверх.

## Запуск

```bash
ros2 launch guide_robot_face face.launch.py
# браузер: http://<host>:8090

ros2 topic echo /face/state --once
# отладка в обход агрегатора:
ros2 service call /face/set_state guide_robot_msgs/srv/SetFaceState \
    "{state: 'thinking', gaze_az: 20.0, seq: 1}"
```

`face_aggregator` выбирает pipeline-состояние (не LLM-affect):
`error` > `driving` > `speaking` > `thinking` > `listening` > `idle`/`sleep`.
`driving` -- `NAVIGATING` и `RETURNING`. Без этого старт тура (thinking)
и транзитный TTS (speaking) перекрывали езду.
Affect (`happy`/…) в этом пакете по-прежнему только через `/face/set_state`.

Из `hardware.launch.py` (по умолчанию включено, проброс `launch_face`):

```bash
ros2 launch guide_robot_bringup hardware.launch.py
# выключить только лицо: launch_face:=false
# выключить весь high-level (голос+карта+миссия+лицо): launch_high_level:=false
```

На ноуте для отладки без переворота поставь `rotate_deg: 0` в YAML.

Превью всех выражений **без ROS**, с той же страницы (`?preview=1`,
`rotate_deg` в браузере сбрасывается в 0). Из каталога `web/`:

```bash
cd guide_robot_face/web
python3 -m http.server 8091
# http://127.0.0.1:8091/?preview=1
# кнопки внизу, ←/→ листают; ?preview=1&state=thinking
```

`web/states.json` — копия YAML для статики; после правки `face_states.yaml`
обнови JSON (`python3 -c "import json,yaml,pathlib; p=pathlib.Path('../config/face_states.yaml'); pathlib.Path('states.json').write_text(json.dumps(yaml.safe_load(p.read_text()), ensure_ascii=False, indent=2)+'\n')"` из `web/`). На роботе `/states.json` по-прежнему отдаёт нода из YAML.

## Состояния

`idle`, `listening`, `thinking`, `speaking`, `driving`, `sleep`, `error`,
`happy`, `surprised`, `curious`, `sad`, `focused`, `shy`.

Неизвестное имя — throttled warning, предыдущее выражение держится,
в браузер не уходит. Добавить новое: блок в `config/face_states.yaml`,
JS не трогать. Комментарий в `FaceState.msg` обновить.

## Как получить рабочее лицо с текущего стейта

Сейчас на роботе крутится `hardware.launch.py` **без** `face_node`
(старый бинарь, нода не в графе). Нужны три слоя: пакет в docker,
HTTP на `:8090`, Firefox kiosk на хосте. Greeter GDM на панели — пока
пользователь `jetson` не залогинен, kiosk не стартует.

### 0. Проверка с ноута (можно сразу после rebuild)

```bash
curl -sS -m 2 -o /dev/null -w '%{http_code}\n' http://10.100.20.169:8090/
# 200 — лицо живо, открывай в браузере. Перевернуто на 180° — так и надо.
ros2 service call /face/set_state guide_robot_msgs/srv/SetFaceState \
    "{state: 'happy', gaze_az: 0.0, seq: 1}"
```

`ros2` с ноута видит джетсон, только если DDS/сеть настроены
(`scripts/ros_dds_lan.sh`). Иначе зови сервис **из контейнера**.

### 1. Passwordless sudo (один раз, пароль спросит именно здесь)

SSH **с TTY**, иначе `sudo` не примет пароль:

```bash
ssh -t jetson@10.100.20.169
```

Проверь, что sudo вообще есть (введи текущий пароль `jetson`):

```bash
sudo -v
```

Дальше без пароля навсегда (лабораторный робот, не интернет-хост):

```bash
echo 'jetson ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/010-jetson-nopasswd
sudo chmod 440 /etc/sudoers.d/010-jetson-nopasswd
sudo visudo -c -f /etc/sudoers.d/010-jetson-nopasswd
```

Уже в этом же SSH проверь, что пароль больше не нужен:

```bash
sudo -k
sudo -n true && echo NOPASSWD_OK
```

`NOPASSWD_OK` — готово. `sudo: a password is required` — snippet не
подхватился: `ls -l /etc/sudoers.d/`, имя файла без точки в начале,
права `0440`, `visudo -c` без ошибок.

Уже не root-ом: `sudo -n true` с ноута (`ssh jetson@10.100.20.169`,
без `-t`) тоже должен проходить.

### 2. GDM autologin (после шага 1, уже без пароля)

```bash
ssh jetson@10.100.20.169
sudo python3 - << 'PY'
from pathlib import Path
p = Path("/etc/gdm3/custom.conf")
text = p.read_text()
# гарантируем ключи в [daemon]
if "AutomaticLoginEnable=true" not in text.replace(" ", ""):
    text = text.replace(
        "[daemon]",
        "[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=jetson",
        1,
    )
p.write_text(text)
print(p.read_text())
PY
```

Или руками: `sudo nano /etc/gdm3/custom.conf`, секция `[daemon]`:

```
[daemon]
WaylandEnable=false
AutomaticLoginEnable=true
AutomaticLogin=jetson
```

Применить (сейчас на экране только greeter — никого не выкинет):

```bash
sudo systemctl restart gdm3
```

После рестарта GDM пользователь `jetson` логинится сам, autostart
запускает `~/.local/bin/face_kiosk.sh` → Firefox kiosk на
`http://127.0.0.1:8090`. Скрипт ждёт порт до 60 с.

Kiosk-файлы на orin-nano уже лежат:

- `~/.local/bin/face_kiosk.sh`
- `~/.config/autostart/guide-robot-face.desktop`

### 3. Лицо в ROS (docker)

Пакеты синкаются в `/home/jetson/Desktop/Projects/Robo-guide` (=
`/home/fabian/ros2_ws/src` в контейнере). Сборка:

```bash
CID=$(docker ps -qf name=robo-guide-jetson | head -1)
docker exec -it "$CID" bash -lc '
  source /opt/ros/humble/setup.bash
  cd /home/fabian/ros2_ws
  colcon build --symlink-install \
    --packages-select guide_robot_msgs guide_robot_face guide_robot_bringup
'
```

Текущий `hardware.launch.py` уже запущен **без** лица. Пока его не
перезапускали — подними ноду рядом (порт 8090 свободен):

```bash
docker exec -d "$CID" bash -lc '
  source /opt/ros/humble/setup.bash
  source /home/fabian/ros2_ws/install/setup.bash
  ros2 launch guide_robot_face face.launch.py
'
```

Следующий полный рестарт `hardware.launch.py` поднимет лицо сам
(`launch_face:=true`). Перед ним останови standalone, иначе два процесса
на `:8090`:

```bash
docker exec "$CID" pkill -f face_node || true
```

### 4. Что должно получиться

| Где | Что видно |
|---|---|
| Браузер на ноуте `http://10.100.20.169:8090` | глаза, вверх ногами |
| DP-панель после autologin | то же, для человека на полу — нормально |
| `ros2 node list \| grep face` (в контейнере) | `/face_node`, `/face_aggregator` |
| сервис `thinking` / `sleep` / `happy` | глаза плавно меняют размер и форму |

Повторная установка kiosk на другую машину:

```bash
mkdir -p ~/.local/bin ~/.config/autostart
cp guide_robot_face/scripts/face_kiosk.sh ~/.local/bin/
chmod +x ~/.local/bin/face_kiosk.sh
cp guide_robot_face/scripts/guide-robot-face.desktop ~/.config/autostart/
# в .desktop: Exec=/home/<user>/.local/bin/face_kiosk.sh
```

## Известные проблемы

- Affect (`happy`/`surprised`/…) агрегатор не ставит: только pipeline
  (`error`/`speaking`/`thinking`/`driving`/`listening`/`idle`/`sleep`).
  LLM JSON / `set_face` tool намеренно нет.
- `error` смотрит только `/supervisor/state == FAULT`. Нет FAULT —
  остальные состояния работают как обычно.
- `/face/set_state` по-прежнему двигает SVG сразу, но следующий кадр
  агрегатора перезапишет `/face/state`.
- HDMI (второй экран) не подключён: в Xorg `DFP-0: disconnected`. Когда
  воткнут — `xrandr` и `--window-position`, не этот пакет.
- `test_copyright` скипается: в исходниках нет copyright header.

## Экран не гаснет по таймауту

Xorg сам гасит панель через 10 мин (`xset q` → Screen Saver timeout 600),
даже если GNOME `idle-delay=0`. С консоли, в графической сессии `jetson`:

```bash
ssh jetson@10.100.20.169
export DISPLAY=:0
export XAUTHORITY=/run/user/1000/gdm/Xauthority
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

# сразу
xset s off
xset s noblank
xset -dpms

# навсегда в dconf пользователя
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
```

На логин (уже кладётся в autostart): `scripts/guide-robot-noblank.desktop`.
На рестарт X — `/etc/X11/xorg.conf.d/10-noblank.conf` (BlankTime 0).
