# Дисплеи Jetson: HDMI-хаб на донгле FL2000 и тач-панель

Файлы отсюда живут на Jetson (`orin-nano`) вне ROS-workspace. Здесь лежат те, что
меняли в сентябре 2026, и инструменты для проверки. Остальная настройка (сервисы
weston/kiosk, статический EDID, udev для донгла) сделана на самом Jetson:

| Что | Где на Jetson |
|---|---|
| Компоситор на донгле | `weston-dlhdmi.service`, `/usr/local/bin/weston-dlhdmi-start`, `/etc/xdg/weston-dlhdmi/weston.ini` (pixman, `rotate-90`) |
| Киоск на HDMI (WebKit GTK, Wayland) | `kiosk-hdmi.service`, `/usr/local/bin/guide-kiosk-hdmi`, URL в `/etc/guide-kiosk/url` |
| Киоск лицевой панели DP-1 (GNOME/X, firefox) | `kiosk-face.service`, ждёт `http://127.0.0.1:8090` (`guide_robot_face`) |
| Драйвер донгла (klogg `fl2000_drm` + `it66121`, DKMS) | `/usr/src/fl2000_drm-1.0`, git-дерево `~/fl2000-build/fl2000_drm` |
| Статический EDID (DDC монитора мёртв) | `fl2000-edid.service`, `/etc/guide-kiosk/fl2000-edid.bin` |
| udev: донгл, сеат, тач | `/etc/udev/rules.d/99-fl2000-hdmi.rules`, `99-guide-kiosk-touch.rules` |

Топология USB: донгл FL2000 (`1d5c:2000`) на USB3-стороне хаба Realtek; тач-панель
Bonxeon `255e:0001` (low-speed, драйвер `usbtouchscreen` из `/usr/src/usbtouchscreen-kiosk`)
на USB2-стороне того же хаба через хаб Genesys и встроенный в монитор хаб Terminus MTT.
Тач назначен на сеат `seat-dlhdmi` (weston), в X его нет, поэтому мышь с GNOME на
HDMI-экран увести нельзя -- это два независимых сеанса.

## Экран гас на секунду при касании (исправлено 2026-09-14)

Причина не в USB, питании или кабеле. `fl2000_stream_compress()` в драйвере переводит кадр
XRGB→RGB888, читая DRM dumb-буфер (CMA, write-combine, без кэша) попиксельно: 130–160 мс на
кадр 1080p. Делала она это под `spin_lock_irq`, а обработчик завершения URB берёт тот же лок
в hardirq на CPU0, где висит единственное прерывание xHCI. Любая перерисовка (касание →
WebKit → коммит weston) замораживала все USB-завершения на ~145 мс; в очереди к донглу
лежат 3 кадра (~33 мс), дальше его FIFO пуст, TMDS рвётся, монитор пересинхронизируется.
В покое коммитов нет, поэтому картинка стояла. Ошибки порта тача, совпадавшие с
vblank-таймаутами, -- следствие того же замораживания.

Измерено ftrace (`xhci_urb_giveback`): до патча 3–5 дыр по 120–160 мс на каждое касание,
после -- за 26 касаний и 2467 перерисовок максимальный интервал между кадрами 50 мс.

`fl2000_streaming_nolock.patch` (относительно DKMS-дерева klogg, уже применён на Jetson,
закоммичен в `~/fl2000-build/fl2000_drm` как `1a1d67f`):

- буфер снимается со списка под локом, конверсия идёт без лока и с включёнными прерываниями;
- каждая строка сначала `memcpy` в кэшируемый буфер, потом конвертируется;
- `spin_lock_irq`/`spin_unlock` → `irqsave`/`irqrestore` (в оригинале completion-путь
  прерывания не восстанавливал);
- пул буферов 4 → 5, `BUG_ON` при пустом `render_list` заменён на пропуск кадра.

Применение (драйвер нельзя перезагрузить на живой системе, снятие weston/драйвера
паникует ядро):

```bash
sudo cp /usr/src/fl2000_drm-1.0/fl2000_streaming.c /usr/src/fl2000_drm-1.0/fl2000_streaming.c.orig
sudo patch -d /usr/src/fl2000_drm-1.0 -p1 < fl2000_streaming_nolock.patch
sudo dkms remove fl2000_drm/1.0 -k "$(uname -r)"     # у dkms 2.8.7 build --force не пересобирает
sudo dkms build  fl2000_drm/1.0 -k "$(uname -r)"
sudo dkms install fl2000_drm/1.0 -k "$(uname -r)"
sudo reboot
```

Проверка: `tools/mon_start.sh` (root) включает ftrace на URB донгла и тача, пишет
`/tmp/mon/trace.txt`, `touchcpu.txt`, `dmesg.txt`; `tools/mon_stop.sh` останавливает и
возвращает ftrace как было. Признак проблемы -- интервалы между `xhci_urb_giveback` кадров
6220800 байт длиннее 30 мс, совпадающие с `TOUCH DOWN` в `touchcpu.txt`.

## Тач перевёрнут на 180° относительно картинки

Панель смонтирована вверх ногами относительно вывода (`transform=rotate-90`). Правило
`99-guide-kiosk-touch.rules` задаёт `LIBINPUT_CALIBRATION_MATRIX="-1 0 1 0 -1 1"`, weston
применяет её при добавлении устройства (в логе `applying calibration`). После правки без
перезагрузки: `udevadm control --reload`, затем unbind/bind интерфейса тача
(`/sys/bus/usb/drivers/usbtouchscreen/`), чтобы weston пересоздал устройство.

## operator_ui на HDMI-экране

`/etc/guide-kiosk/url` = `http://127.0.0.1:8091` (`guide_robot_operator_ui`, порт из
`config/operator_ui.yaml`). Нода запускается в dev-контейнере, отдельно от `hardware.launch`:

```bash
docker start robo-guide-jetson-1
docker exec -d robo-guide-jetson-1 bash -c \
  "cd /home/fabian/ros2_ws && source install/setup.bash && \
   exec ros2 launch guide_robot_operator_ui operator_ui.launch.py > /tmp/operator_ui.log 2>&1"
```

## Известные проблемы

- Нода `operator_ui` не переживает перезагрузку Jetson: нет systemd-юнита, запуск руками.
- В образе `fabook/iros:jetson` нет `pyserial`; RFID-бэкенд `operator_ui` упадёт на
  `import serial`, как только появится `rfid_secret` и `/dev/rfid0`.
- Чекаут `~/Desktop/Projects/Robo-guide` на Jetson отстаёт от `dev` и несёт незакоммиченные
  правки (nav2, robot_params, supervisor, audio); `guide_robot_operator_ui` и
  `guide_robot_msgs` подтянуты туда точечно (`git checkout origin/dev -- ...`).
- Git-дерево драйвера `~/fl2000-build/fl2000_drm` отстаёт от DKMS-дерева: там ещё
  `FL2000_PPM_ERR_MAX 8000` и `drm_fbdev_generic_setup`, в `/usr/src` уже 2000 и без fbdev.
- На встроенном хабе монитора (Terminus, порт 1) сидит устройство, которое не
  энумерируется (`Cannot enable... attempt power cycle`). Пока безвредно.
- `weston-screenshooter` с этого сеата не работает («unauthorized»): протокол
  привилегированный, клиент пускается только по горячей клавише weston, а клавиатуры нет.
