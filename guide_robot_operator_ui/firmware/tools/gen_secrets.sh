#!/usr/bin/env bash
# gen_secrets.sh -- генерирует заголовки секретов для rfid_bridge и
# card_provision из ~/.config/guide_robot/ (design E4/E5).
#
# Ключи создаются ОДИН РАЗ и только если файлов ещё нет. Скрипт никогда
# не перезаписывает существующие ключи: перегенерация rfid_sector_key
# делает все ранее прошитые карты нечитаемыми, без возможности вернуть.
#
# Запуск из любого места:
#   ./tools/gen_secrets.sh
set -euo pipefail

CONF_DIR="${HOME}/.config/guide_robot"
SECRET_FILE="${CONF_DIR}/rfid_secret"
KEY_FILE="${CONF_DIR}/rfid_sector_key"
FW_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "${CONF_DIR}"
chmod 700 "${CONF_DIR}"

# 1. Общий секрет -- 32 байта. Без перевода строки: прошивка считает HMAC
# от строкового литерала через strlen(), и лишний \n в копии узла разведёт
# подписи. Симптом был бы "карта читается, но вход не проходит".
if [[ -f "${SECRET_FILE}" ]]; then
  echo "есть: ${SECRET_FILE} (не трогаю)"
else
  openssl rand -hex 32 | tr -d '\n' > "${SECRET_FILE}"
  chmod 600 "${SECRET_FILE}"
  echo "создан: ${SECRET_FILE}"
fi

# 2. Ключ сектора MIFARE -- 6 байт.
if [[ -f "${KEY_FILE}" ]]; then
  echo "есть: ${KEY_FILE} (не трогаю)"
else
  openssl rand -hex 6 | tr -d '\n' > "${KEY_FILE}"
  chmod 600 "${KEY_FILE}"
  echo "создан: ${KEY_FILE}"
  echo
  echo "!! ЗАПИШИ ЭТОТ КЛЮЧ ОТДЕЛЬНО: $(cat "${KEY_FILE}")"
  echo "   Потеря = все прошитые им карты мертвы навсегда."
fi

SECRET="$(cat "${SECRET_FILE}")"
KEY_HEX="$(cat "${KEY_FILE}")"

if [[ ! "${SECRET}" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "ОШИБКА: ${SECRET_FILE} -- ожидалось 64 hex-символа без перевода строки" >&2
  exit 1
fi
if [[ ! "${KEY_HEX}" =~ ^[0-9a-fA-F]{12}$ ]]; then
  echo "ОШИБКА: ${KEY_FILE} -- ожидалось 12 hex-символов без перевода строки" >&2
  exit 1
fi

KEY_C="$(echo -n "${KEY_HEX}" | sed 's/../0x&, /g; s/, $//')"

cat > "${FW_DIR}/rfid_bridge/include/rfid_bridge_secrets.h" <<EOF
// СГЕНЕРИРОВАНО tools/gen_secrets.sh -- не редактировать, не коммитить.
#pragma once

#define RFID_BRIDGE_SHARED_SECRET "${SECRET}"

#define RFID_BRIDGE_SECTOR_KEY \\
  { ${KEY_C} }
EOF
chmod 600 "${FW_DIR}/rfid_bridge/include/rfid_bridge_secrets.h"

cat > "${FW_DIR}/card_provision/include/card_provision_secrets.h" <<EOF
// СГЕНЕРИРОВАНО tools/gen_secrets.sh -- не редактировать, не коммитить.
#pragma once

#define RFID_SECTOR_KEY \\
  { ${KEY_C} }
EOF
chmod 600 "${FW_DIR}/card_provision/include/card_provision_secrets.h"

echo
echo "заголовки записаны:"
echo "  rfid_bridge/include/rfid_bridge_secrets.h"
echo "  card_provision/include/card_provision_secrets.h"
echo
if ! git -C "${FW_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
  echo "(не git-репозиторий -- проверку .gitignore пропускаю)"
  exit 0
fi

echo "проверка, что они не попадут в git:"
rc=0
for f in rfid_bridge/include/rfid_bridge_secrets.h \
         card_provision/include/card_provision_secrets.h; do
  if git -C "${FW_DIR}" check-ignore -q "${FW_DIR}/${f}"; then
    echo "  OK  ${f}"
  else
    echo "  !!! ${f} НЕ игнорируется git -- НЕ КОММИТЬ, проверь .gitignore"
    rc=1
  fi
done
exit "${rc}"
