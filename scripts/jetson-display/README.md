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

## Панель на HDMI-экране

`/etc/guide-kiosk/url-hdmi` = `http://127.0.0.1:8089` (хостовый сервис,
опциональный `reference_ui`). Лицо использует `/etc/guide-kiosk/url` и порт 8090.
Приватный `guide_robot_api` на 8091 не обслуживает браузерные страницы.
См. [установку хостового сервиса](../jetson-launcher/README.md).

## Исторические наблюдения до миграции API v1

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

## Optional GTK3 → Weston text-input-v1 bridge

The Jetson currently uses Weston 13 desktop-shell and WebKitGTK/GTK3. Its
`weston-keyboard` runs already, but Weston advertises text-input-v1 while GTK's
standard Wayland IM module uses v3; the kiosk previously loaded IBus.

`kiosk-textinput-v1` builds the LGPL GTK module from
https://github.com/ashie/gtk-wayland-textinputv1 at pinned revision
`79b23b2487462e2f219e79b68afd75d14e671cf1`. Run as the normal Jetson user:

```sh
./scripts/jetson-display/kiosk-textinput-v1 install
./scripts/jetson-display/kiosk-textinput-v1 enable
./scripts/jetson-display/kiosk-textinput-v1 status
# Roll back the kiosk-specific environment:
./scripts/jetson-display/kiosk-textinput-v1 disable
```

Build dependencies: git, gcc, make, autotools/libtool, pkg-config, GTK3 and
Wayland development headers, wayland-protocols and wayland-scanner. The script
uses an isolated cache under `/opt/guide-kiosk-input`; it does not modify the
system GTK cache. Only kiosk-hdmi is restarted; never restart Weston for this.
The user must verify touch focus, SSID/password entry, backspace, selection,
field switching, keyboard dismissal and required languages/special characters.
Retain the web keyboard until this acceptance succeeds. Upstream Weston keyboard
layouts may not cover Russian or all password symbols. This is an opt-in trial,
not part of the default launcher installer.

Touch-only seats need the matching `guide-kiosk-hdmi` wrapper: GTK may report an
inactive toplevel despite HTML focus. When `GTK_IM_MODULE=wltextinputv1`, the
wrapper forwards top-frame editable DOM focus (a boolean, never field text) to
WebKit's existing input method context. Other IM modules retain normal behavior.
Install the wrapper from this repository to `/usr/local/bin/guide-kiosk-hdmi`
with mode 755 before enabling the integration. The module-only first trial did
not open the keyboard; an explicit WebKit IM focus probe produced
show_input_panel/activate and a compositor enter acknowledgment.

### Physical keyboard trial

`99-guide-kiosk-keyboard.rules` optionally assigns only the Logitech C31C main
keyboard interface on USB path `2.4.1.4:1.0` to `seat-dlhdmi`. It otherwise belongs
to seat0 (GNOME / face display). Install this rule in `/etc/udev/rules.d/`, reload
udev rules and reconnect that keyboard. Do not restart Weston. This is a physical
keyboard trial, not a touch-only solution: Weston 13's input-panel code requires
a seat with keyboard focus to map the input-panel view. Jetson's current kernel
has `CONFIG_INPUT_UINPUT` disabled. Keep the web keyboard until touch-only operation
and the native keyboard's character coverage have been addressed.

The installer applies `patches/0001-preedit-lifecycle.patch` to the pinned GTK
module. Upstream emitted preedit-end for every non-empty preedit string, causing
WebKit to discard the visible composition. The patch ends composition only on
the non-empty → empty transition and releases old styling attributes. The
headless regression `tests/test_preedit.py SOURCE_TREE` compiles the actual
callbacks against GTK signals (only transport updates are stubbed); it fails
on upstream and verifies typing, backspace, empty updates, UTF-8 commit,
focus-out commit and stale serial rejection on the patched source. Installation
runs it before building/replacing the module, and replaces the .so atomically
so an existing kiosk process retains its original mapping until restart.
