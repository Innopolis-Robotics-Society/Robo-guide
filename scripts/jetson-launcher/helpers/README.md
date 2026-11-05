# Системные помощники

HTTP API остаётся непривилегированным. Помощники слушают только Unix sockets,
доступные пользователю API через группу `guide-control` (режим socket 0660).
Не выдавайте доступ к этой группе другим пользователям.

`audio_service.py` запускается user-unit `guide-audio.service` в сессии владельца
PulseAudio/PipeWire. Требуются Python aiohttp и pactl. Файл
`~/.config/guide-audio.env` содержит:

```
GUIDE_AUDIO_SINK=<точное имя выхода pactl>
GUIDE_AUDIO_SOCKET=/run/user/<uid>/guide-audio.sock
GUIDE_AUDIO_MAX_PERCENT=20
```

Путь socket указывается в launcher `audio_socket`. Для доступа системного
launcher к пользовательскому runtime directory настройте ACL только для его
пользователя; не делайте весь каталог общедоступным. Файл состояния располагается
в ~/.local/state/guide-audio.json. Служба маршрутизации jetson_audio.sh при
GUIDE_AUDIO_MANAGED=1 перестаёт устанавливать громкость и mute по таймеру.
Включать этот флаг только после запуска audio helper.

`wifi_service.py` требует aiohttp, dbus-next и NetworkManager. Установить
`guide-wifi.service` как system unit. /etc/guide-launcher/wifi.env:

```
GUIDE_WIFI_INTERFACE=wlan0
GUIDE_WIFI_HOTSPOT_INTERFACE=wlan1
GUIDE_WIFI_SOCKET=/run/guide-wifi/control.sock
```

Настроить реальные имена интерфейсов: помощник запрещает USB-адаптеры и адаптер
служебной точки доступа. `wifi_socket` launcher — тот же путь. Пароли в env не
нужны: их сохраняет NetworkManager. Сервис root ограничен закрытым набором
методов одного адаптера; web-сервис root не запускается.

Включение служб и смену текущей сети выполнять на роботе при остановленной миссии.
Пустые audio_socket/wifi_socket явно отключают соответствующий API (503).

При миграции старой установки отключите прежние службы автоподключения
(на Jetson это `robo-wifi.service`). На основном управляемом адаптере
профили с `802-11-wireless.mode=ap` не должны иметь `connection.autoconnect=yes`:
иначе NetworkManager может включить hotspot после загрузки или неудачного
подключения. Это проверяется отдельно от служебной USB-точки доступа;
не отключайте её профиль при исправлении основного адаптера.
Помощник восстанавливает только профиль из разрешённого списка:
infrastructure, выбранный интерфейс, поддерживаемая защита. Hotspot и профили
других адаптеров не восстанавливаются ни при отказе, ни при исключении D-Bus.
