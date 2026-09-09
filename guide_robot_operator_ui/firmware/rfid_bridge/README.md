# rfid_bridge — прошивка RC522↔ESP32-S3 (Task E4/E5)

Мост между RFID-ридером и `guide_robot_operator_ui`: по USB CDC отвечает на
`{"cmd":"challenge","nonce":...}` HMAC-подписью, если поднесённая карта
аутентифицируется по защищённому сектору. Протокол и граница доверия —
`guide_robot_operator_ui/README.md`, раздел «Аутентификация»; здесь —
только сборка и прошивка железа.

**Без прошитого секрета/ключа сектора нода поднимается, но RFID
недоступен, вход только по PIN** (`rfid_secret_file` пуст по умолчанию) —
собирать эту прошивку не обязательно, чтобы работать с панелью.

## Железо

- ESP32-S3 (нативный USB, важно — не ESP32 без USB-OTG)
- RC522 (MFRC522, SPI) — либо клон FM17522, см. ниже
- Карты MIFARE Classic 1K, прошитые утилитой `../card_provision`

### Распиновка

Проверена на `esp32-s3-devkitc-1-N8`.

| RC522 | ESP32-S3 |
|---|---|
| SDA (SS) | GPIO10 |
| SCK | GPIO12 |
| MOSI | GPIO11 |
| MISO | GPIO13 |
| RST | GPIO9 |
| 3.3V | 3.3V — **не 5V**, модуль умрёт молча |
| GND | GND |

SCK/MOSI/MISO не задаются в коде: берутся из `variants/esp32s3/pins_arduino.h`
ядра Arduino-ESP32 (`SS=10, MOSI=11, MISO=13, SCK=12`), что совпадает с
таблицей выше. `PIN_RC522_SS`/`PIN_RC522_RST` в `src/main.cpp` заданы явно —
меняй их, если разводишь иначе. IRQ модуля не подключается.

Повесь на VCC модуля 100 нФ + 10–47 мкФ. Без развязки будут спонтанные
ресеты при поднесении карты — симптом «работает через раз».

## Среда сборки

[PlatformIO](https://platformio.org/) — CLI (`pip install platformio`) или
расширение VS Code. Библиотека `miguelbalboa/MFRC522` подтягивается
автоматически (`lib_deps`).

## Секреты — обязательно перед сборкой

```bash
../tools/gen_secrets.sh
```

Скрипт создаёт (один раз, не перезаписывая существующие)
`~/.config/guide_robot/rfid_secret` и `~/.config/guide_robot/rfid_sector_key`,
затем генерирует из них `include/rfid_bridge_secrets.h` и
`../card_provision/include/card_provision_secrets.h`.

Руками эти заголовки не редактируются. Расхождение в один символ между
прошивкой и `rfid_secret_file` узла даёт «карта не читается», и искать
будешь со стороны антенны.

Серийник USB — не секрет, он лежит открытым текстом в коммитящемся
`scripts/99-guide-robot-rfid.rules`. Задаётся `-D USB_SERIAL` в
`platformio.ini`, **уникальный на весь парк**, и должен совпадать с
`ATTRS{serial}` в правиле.

## Прошивка

```bash
pio run -t clean && pio run -t upload
```

Кабель — в разъём **`USB`**, не `UART`: `Serial` здесь TinyUSB CDC на
нативном USB, через мост CP2102 его не видно, и `USB_SERIAL` там ни на что
не влияет. Если порт не найден: зажать `BOOT`, нажать `RESET`, отпустить
`BOOT`.

Проверка сразу после — перетыкнуть кабель физически (дескриптор кэшируется
хостом) и:

```bash
lsusb -d 303a: -v 2>/dev/null | grep -iE 'iProduct|iSerial'
```

Ожидается `iSerial: guide-robot-rfid-01` и `iProduct` с именем платы.
`iProduct: USB JTAG/serial debug unit` означает, что `ARDUINO_USB_MODE=0`
не доехал до компилятора — см. следующий раздел.

### Почему в platformio.ini переопределён board_build.extra_flags

Манифест платы (`~/.platformio/platforms/espressif32/boards/esp32-s3-devkitc-1.json`)
жёстко задаёт `-DARDUINO_USB_MODE=1` в `build.extra_flags`, а они
подставляются **после** `build_flags`. При двух одинаковых `-D` побеждает
последний, поэтому `-DARDUINO_USB_MODE=0` из `build_flags` перетирается
молча, без предупреждения. `build_unflags` не помогает: он не доходит до
сборки ядра Arduino, а `USB.begin()` живёт именно там
(`cores/esp32/main.cpp`, под `#if ARDUINO_USB_ON_BOOT && !ARDUINO_USB_MODE`).
Поэтому список флагов платы переопределён целиком, без `USB_MODE`.

Проверить, что define доехал, до прошивки:

```bash
pio run -t clean
pio run -v 2>&1 | grep -o 'ARDUINO_USB_MODE=[01]' | sort -u   # ждём только =0
```

При `MODE=1` плата перечисляется как `USB JTAG/serial debug unit`, а
`iSerial` равен MAC-адресу — по нему udev-правило не сматчится.

### Почему серийник не задаётся из setup()

`ESPUSB::serialNumber()` после старта TinyUSB молча возвращает `false`:

```c
bool ESPUSB::serialNumber(const char * name){
    if(!_started){ serial_number = name; }
    return !_started;
}
```

А `USB.begin()` ядро зовёт в `app_main()`, то есть до `setup()`. Значит
любой вызов из `setup()` бесполезен при любых флагах. Серийник задаётся
только на компиляции, через `-D USB_SERIAL`, который перебивает дефолт
`"__MAC__"` в `cores/esp32/USB.cpp`.

## Проверка моста без ROS

```bash
pip install pyserial
../tools/challenge_test.py /dev/ttyACM0     # до udev
../tools/challenge_test.py                  # после udev, /dev/rfid0
```

Ждём `card`, `HMAC: совпал`, `E4: ок`. Появление UID в любом поле ответа —
нарушение E4, граница доверия сломана.

## Опрос вероятностный — узлу нужен retry

Прошивка делает **ровно один REQA на challenge**, синхронно. Попадание
карты в поле в этот момент не гарантировано: на живом железе (FM17522)
наблюдалось три подряд `no_card` перед успехом.

Плюс `readOperatorName()` вызывает `PICC_HaltA()` на каждом выходе, а
`PICC_IsNewCardPresent()` шлёт REQA, на который карта в состоянии HALT не
отвечает. Следствие: второй challenge подряд при неподвижно лежащей карте
штатно вернёт `no_card`.

**Узел обязан опрашивать мост в цикле с retry.** Single-shot реализация
даст вход по RFID «через раз» и будет выглядеть как неисправный ридер.

## Чужие и чистые карты

Карта без прошитого сектора, клон по UID и чужая карта — все дают
`{"ok":false,"err":"no_card"}`, неотличимо от отсутствия карты. Это по
замыслу (design E4: клонировать UID недостаточно), не неисправность.

## Клон FM17522

`VersionReg` на многих модулях возвращает `0x88` — это Fudan FM17522, клон
RC522. Библиотека знает его явно (`case 0x88: // Fudan Semiconductor
FM17522 clone`), работает штатно. Дальность меньше оригинала, 2–3 см с
толстой картой. `0x00` или `0xFF` — вот это отказ связи по SPI.

Диагностика SPI отдельно от логики моста: прошей `../card_provision`, он
печатает `VersionReg` при старте.

## udev

```bash
sudo cp ../../scripts/99-guide-robot-rfid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# перетыкнуть кабель, затем:
ls -l /dev/rfid0
ls -l /dev/ttyACM0     # ждём crw-rw---- root dialout
id -nG "$USER" | grep -o dialout
```

Правило матчится по `ATTRS{serial}`, не по `idVendor`/`idProduct` — те у
ESP32-S3 одни и те же (`303a:1001`) на тысячах устройств, а `/dev/ttyACM*`
перенумеровывается при каждом переподключении. Симлинк создаётся только
при (пере)подключении: после `udevadm trigger` на уже воткнутом устройстве
его может не быть — это нормально, перетыкни кабель.

Узел работает не от root, доступ к `/dev/rfid0` нужен твоему пользователю
(группа `dialout`; при необходимости `sudo usermod -aG dialout $USER` и
перелогиниться).

## Известное ограничение

Схема поднимает планку с «клонировал UID за три секунды» до «нужна
nested-атака на Crypto1», но криптографической аутентификацией карты не
является — подробности и обоснование в `guide_robot_operator_ui/README.md`,
раздел «Аутентификация» → «RFID — известное ограничение».
