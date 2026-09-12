# RFID — шпаргалка

Две прошивки на одной плате. Что залито — то и работает; переключение
через `pio run -t upload` в нужном каталоге.

| Каталог | Для чего | Серийник USB |
|---|---|---|
| `rfid_bridge/` | рабочий режим: узел читает карты | `guide-robot-rfid-01` |
| `card_provision/` | выпуск и проверка карт | `guide-robot-card-provision` |

Пока залит `card_provision`, `/dev/rfid0` не существует — udev-правило
матчится по серийнику. Это нарочно, чтобы узел не открыл REPL утилиты.

**Что залито прямо сейчас:**

```bash
lsusb -d 303a: -v 2>/dev/null | grep -i iserial
```

Кабель всегда в разъём `USB`, не `UART`.

---

## Выпустить новую карту

```bash
cd firmware/card_provision
pio run -t upload && pio device monitor
```

Баннер с `VersionReg` = утилита залита. Дальше в мониторе:

```
write op_fabian
```

Поднести **чистую** карту вплотную к антенне. Ждём:

```
   блок 4 записан
   trailer записан, ключ сменён
```

Убрать карту из поля, поднести заново, набрать `verify`. Ждём
`OK: прошитая карта, имя = 'op_fabian'`.

Имя: ASCII, до 16 символов, соглашение `op_<логин>`.

## Сменить имя на существующей карте

Та же прошивка, но `rename` вместо `write` — ключ не трогается:

```
rename op_newname
```

`write` на прошитой карте откажет (заводской ключ уже не подходит) и
ничего не запишет.

## Проверить карту

```
verify
```

- `OK: прошитая карта, имя = '...'` — рабочая
- `ЧИСТАЯ карта` — под `write`
- `НИ ОДИН ключ не подошёл` — чужая, другой ключ или не MIFARE Classic 1K

## Вернуть рабочий режим

```bash
cd firmware/rfid_bridge
pio run -t upload
```

Перетыкнуть кабель, затем:

```bash
ls -l /dev/rfid0
../tools/challenge_test.py
```

Ждём `card`, `HMAC: совпал`, `E4: ок`.

---

## Первый запуск на новой машине

```bash
pip install platformio pyserial
cd firmware
./tools/gen_secrets.sh
```

Скрипт создаёт ключи **только если их нет**. Если на машине уже
выпускались карты — ключи обязаны быть теми же, иначе карты не
прочитаются. Копируй `~/.config/guide_robot/{rfid_secret,rfid_sector_key}`
с рабочей машины, а не генерируй заново.

udev (один раз):

```bash
sudo cp ../scripts/99-guide-robot-rfid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# перетыкнуть кабель
ls -l /dev/rfid0
id -nG "$USER" | grep -o dialout
```

---

## Если не работает

**Ошибка сборки `card_provision_secrets.h: No such file`** — не запускал
`./tools/gen_secrets.sh`. Заголовки в `.gitignore`, в репозитории их нет.

**В мониторе приходит `{"ok":false,"err":"bad_request"}`** — залит
`rfid_bridge`, а не утилита. Перепрошей.

**`no_card` при поднесённой карте** — норма, опрос вероятностный. Прошивка
делает один REQA на запрос, и `PICC_HaltA()` уводит карту в состояние,
где она не отвечает на повторный REQA. Убери карту и поднеси заново;
`challenge_test.py` уже делает 20 попыток.

**Дальность** — 2–3 см, часто меньше. Клади карту на антенну, не подноси
издалека. Металл ближе 10 мм за антенной убивает поле.

**`VersionReg = 0x00` или `0xFF`** — SPI не работает. Проверь SS→GPIO10,
MISO→GPIO13, питание **3.3 В** (не 5 В — модуль умрёт молча), общую землю.
`0x88` это FM17522, клон, нормально.

**Карта не читается после смены ключа** — сверь:

```bash
grep -A2 SECTOR_KEY rfid_bridge/include/rfid_bridge_secrets.h \
  | grep -o '0x[0-9a-f]\{2\}' | tr -d '\n' | sed 's/0x//g'; echo
cat ~/.config/guide_robot/rfid_sector_key; echo
```

---

## Не потеряй

`~/.config/guide_robot/rfid_sector_key` невосстановим. Потеряешь — все
выпущенные карты мертвы навсегда. Держи копию вне машины.

Байты доступа `FF 07 80 69` в `card_provision/src/main.cpp` не менять:
любое другое значение необратимо кирпичит сектор карты.

Подробности — `rfid_bridge/README.md` и `card_provision/README.md`.
