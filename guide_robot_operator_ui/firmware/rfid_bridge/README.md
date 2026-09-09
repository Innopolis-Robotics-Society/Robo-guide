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
- RC522 (MFRC522, SPI)
- Карты MIFARE Classic 1K с прошитым сектором 1 (см. «Подготовка карт» ниже)

### Распиновка (по умолчанию в `src/main.cpp`)

| RC522 | ESP32-S3 |
|---|---|
| SDA (SS) | GPIO10 |
| SCK | GPIO12 (аппаратный SPI2 SCK) |
| MOSI | GPIO11 |
| MISO | GPIO13 |
| RST | GPIO9 |
| 3.3V | 3.3V |
| GND | GND |

SCK/MOSI/MISO выше — типичные дефолтные пины аппаратного SPI (FSPI) для
`esp32-s3-devkitc-1` в Arduino-ESP32 core; **сверь с распиновкой своей
конкретной платы** до пайки — они не заданы явно в коде, а берутся
дефолтными для `board` из `platformio.ini`. `PIN_RC522_SS`/`PIN_RC522_RST`
в `src/main.cpp` — единственное, что задано явно, меняй их под свою схему.

## Среда сборки

[PlatformIO](https://platformio.org/) — CLI (`pip install platformio`) или
расширение VS Code. Библиотека `miguelbalboa/MFRC522` подтягивается
автоматически (`lib_deps` в `platformio.ini`).

## Секреты — обязательно перед сборкой

```bash
cp include/rfid_bridge_secrets.h.example include/rfid_bridge_secrets.h
```

и отредактировать три значения в `rfid_bridge_secrets.h` (файл в
`.gitignore`, реальные значения в git не попадают):

1. **`RFID_BRIDGE_SHARED_SECRET`** — тот же секрет, что кладётся в файл,
   на который указывает `rfid_secret_file` в `config/operator_ui.yaml` на
   стороне узла. `openssl rand -hex 32`.
2. **`RFID_BRIDGE_SECTOR_KEY`** — ключ сектора MIFARE (Key A), которым
   прошиваются карты операторов (не заводской `FFFFFFFFFFFF`). 6 байт,
   любой сгенерированный ключ, например через `nfc-mfclassic`/`mfoc`.
3. **`RFID_BRIDGE_USB_SERIAL`** — уникальная строка для ЭТОЙ платы (не
   одна на весь парк роботов). Без неё все ESP32-S3 отвечают одним и тем
   же `303a:1001`, и udev-правило (`../../scripts/99-guide-robot-rfid.rules`)
   не сможет отличить один ридер от другого.

## Прошивка

Из этого каталога (`firmware/rfid_bridge/`):

```bash
pio run -t upload
pio device monitor    # проверка: держать карту к ридеру, смотреть ответы
```

## Подготовка карт операторов

Сектор 1, блок 4 карты должен содержать ASCII-имя оператора (до 16 байт,
дополненное нулями) — то, что уйдёт в поле `"card"` протокола и в jsonl-лог
как `operator`. Записывается ключом `RFID_BRIDGE_SECTOR_KEY` любым
MIFARE-инструментом (`nfc-mfclassic`, приложение NFC Tools и т.п.):

```
блок 4, сектор 1, ключ A = RFID_BRIDGE_SECTOR_KEY:
  "op_mook\0\0\0\0\0\0\0\0\0"   (16 байт, null-padded)
```

Карта без этого ключа/данных не аутентифицируется вообще — ридер отвечает
`{"ok":false,"err":"no_card"}`, неотличимо от отсутствия карты.

## udev

Правило — `../../scripts/99-guide-robot-rfid.rules`, ставится по образцу
kiosk-рецепта `guide_robot_face/README.md` (ручная установка, не часть
сборки пакета):

```bash
sudo cp scripts/99-guide-robot-rfid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# проверка:
ls -l /dev/rfid0
```

Правило матчится по `ATTRS{serial}` (значению `RFID_BRIDGE_USB_SERIAL`
выше), не по `idVendor`/`idProduct` — те у ESP32-S3 одни и те же
(`303a:1001`) на тысячах устройств, `/dev/ttyACM*` при этом
перенумеровывается при каждом переподключении.

## Известное ограничение

Схема поднимает планку с «клонировал UID за три секунды» до «нужна
nested-атака на Crypto1», но криптографической аутентификацией карты не
является — подробности и обоснование в `guide_robot_operator_ui/README.md`,
раздел «Аутентификация» → «RFID — известное ограничение».
