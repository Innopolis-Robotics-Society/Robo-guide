#!/usr/bin/env bash
# Установка guide-launcher на Jetson. Запуск: sudo scripts/jetson-launcher/install.sh
#   --preflight-only  только проверки, ничего не менять
#   DRY_RUN=1         напечатать действия установки, ничего не менять
# Идемпотентен. Preflight ничего не угадывает: любая расходимость -> exit 1 до первого изменения
# (единственное исключение -- apt-get install недостающих python3-пакетов, они нужны самому preflight).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$REPO_DIR/scripts/jetson-launcher"
DISPLAY_DIR="$REPO_DIR/scripts/jetson-display"

CONTAINER="${CONTAINER:-robo-guide-jetson-1}"
SERVICE_USER="${SERVICE_USER:-jetson}"
CONTAINER_USER="${CONTAINER_USER:-fabian}"
ETC_DIR="${ETC_DIR:-/etc}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
OPT_DIR="${OPT_DIR:-/opt/guide-launcher}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
HOST_PYTHON="${HOST_PYTHON:-python3}"
FACE_URL="http://127.0.0.1:8090"
HDMI_URL="http://127.0.0.1:8089"
PORT=8089
DRY_RUN="${DRY_RUN:-0}"
PREFLIGHT_ONLY=0
[ "${1:-}" = "--preflight-only" ] && PREFLIGHT_ONLY=1

FAILS=()
pass() { printf '  [ OK ] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILS+=("$1"); }
note() { printf '         %s\n' "$1"; }
die() { printf 'install.sh: %s\n' "$1" >&2; exit 1; }

if [ "$PREFLIGHT_ONLY" = 0 ] && [ "$DRY_RUN" != 1 ] && [ "$(id -u)" != 0 ]; then
  die "нужен root: sudo $0"
fi

deps_ok() { "$HOST_PYTHON" -c 'import aiohttp, yaml, serial' >/dev/null 2>&1; }

echo "== Зависимости хоста"
if deps_ok; then
  pass "aiohttp, yaml, serial импортируются"
elif [ "$PREFLIGHT_ONLY" = 1 ] || [ "$DRY_RUN" = 1 ]; then
  fail "не хватает python3-aiohttp / python3-yaml / python3-serial (install.sh поставит через apt)"
else
  echo "  apt-get install python3-aiohttp python3-yaml python3-serial"
  apt-get install -y python3-aiohttp python3-yaml python3-serial
  deps_ok || die "после apt-get пакеты всё ещё не импортируются"
  pass "python3-пакеты установлены через apt"
fi

echo "== Контейнер $CONTAINER"
INSPECT="$(docker inspect "$CONTAINER" 2>/dev/null || true)"
if [ -z "$INSPECT" ] || [ "$INSPECT" = "[]" ]; then
  fail "контейнер $CONTAINER не найден: docker compose up -d jetson"
else
  INSPECT_PY="$(cat <<'PY'
import json, os, sys
repo, compose = sys.argv[1], sys.argv[2]
info = json.load(sys.stdin)[0]
out = []
if not info["State"]["Running"]:
    out.append("FAIL контейнер не запущен: docker start или docker compose up -d jetson")
if info["HostConfig"]["NetworkMode"] == "host":
    out.append("OK NetworkMode=host (:8091 виден с хоста по 127.0.0.1)")
else:
    out.append("FAIL NetworkMode=%s, нужен host" % info["HostConfig"]["NetworkMode"])
mounts = {(m["Destination"], os.path.realpath(m["Source"])) for m in info["Mounts"]}
if ("/home/fabian/ros2_ws/src", os.path.realpath(repo)) in mounts:
    out.append("OK репозиторий смонтирован в /home/fabian/ros2_ws/src")
else:
    out.append("FAIL репозиторий %s не смонтирован в /home/fabian/ros2_ws/src" % repo)
if any(dest == "/dev" for dest, _ in mounts):
    out.append("OK /dev смонтирован")
else:
    out.append("FAIL /dev не смонтирован")
try:
    import yaml
    want = yaml.safe_load(open(compose))["services"]["jetson"]["command"]
    if info["Config"]["Cmd"] == want:
        out.append("OK Config.Cmd совпадает с compose.yaml")
    else:
        out.append("FAIL Config.Cmd отличается от compose.yaml: остановите стек из меню, затем: "
                   "docker compose up -d --force-recreate jetson")
except ImportError:
    out.append("FAIL нет yaml: Config.Cmd не проверить")
print("\n".join(out))
PY
)"
  RESULT="$(python3 -c "$INSPECT_PY" "$REPO_DIR" "$REPO_DIR/compose.yaml" <<<"$INSPECT" || true)"
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in OK\ *) pass "${line#OK }" ;; FAIL\ *) fail "${line#FAIL }" ;; esac
  done <<<"$RESULT"
  [ -n "$RESULT" ] || fail "не удалось разобрать docker inspect"
fi

echo "== Пользователь и порт"
if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  pass "$SERVICE_USER в группе docker"
else
  fail "$SERVICE_USER не в группе docker: sudo usermod -aG docker $SERVICE_USER, затем перелогиниться"
fi

if systemctl is-active --quiet guide-launcher 2>/dev/null; then
  pass "порт $PORT занят самим guide-launcher (переустановка)"
elif ss -ltn 2>/dev/null | grep -qE "[:.]$PORT[[:space:]]"; then
  fail "порт $PORT занят другим процессом: ss -ltnp | grep $PORT"
else
  pass "порт $PORT свободен"
fi

echo "== Киоски"
URL_FILE="$ETC_DIR/guide-kiosk/url"
if [ -f "$URL_FILE" ]; then
  pass "$URL_FILE существует, сейчас: $(tr -d '\n' <"$URL_FILE")"
else
  fail "нет $URL_FILE"
fi
if systemctl cat kiosk-hdmi.service >/dev/null 2>&1; then
  pass "kiosk-hdmi.service существует"
else
  fail "нет kiosk-hdmi.service"
fi

INSTALLED_HDMI="$BIN_DIR/guide-kiosk-hdmi"
if [ ! -f "$INSTALLED_HDMI" ]; then
  fail "нет $INSTALLED_HDMI"
else
  MATCH=""
  cmp -s "$INSTALLED_HDMI" "$DISPLAY_DIR/guide-kiosk-hdmi" && MATCH="рабочая копия репозитория"
  if [ -z "$MATCH" ]; then
    for rev in $(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" log --format=%H -- scripts/jetson-display/guide-kiosk-hdmi 2>/dev/null); do
      if git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" show "$rev:scripts/jetson-display/guide-kiosk-hdmi" 2>/dev/null | cmp -s - "$INSTALLED_HDMI"; then
        MATCH="ревизия ${rev:0:8}"
        break
      fi
    done
  fi
  if [ -n "$MATCH" ]; then
    pass "$INSTALLED_HDMI известен репозиторию ($MATCH)"
  else
    fail "$INSTALLED_HDMI не совпадает ни с одной версией из репозитория (diff ниже)"
    diff -u "$INSTALLED_HDMI" "$DISPLAY_DIR/guide-kiosk-hdmi" | sed 's/^/         /' || true
  fi
fi

FACE_SCRIPT="$BIN_DIR/guide-kiosk-face"
if [ ! -f "$FACE_SCRIPT" ]; then
  fail "нет $FACE_SCRIPT"
else
  note "grep -n url $FACE_SCRIPT:"
  grep -n url "$FACE_SCRIPT" | sed 's/^/           /' || true
  if grep -q "guide-kiosk/url" "$FACE_SCRIPT"; then
    pass "guide-kiosk-face читает /etc/guide-kiosk/url (после установки там будет $FACE_URL)"
  else
    fail "guide-kiosk-face берёт адрес не из /etc/guide-kiosk/url: ничего не менять, разберитесь вручную"
  fi
fi

echo "== Окружение и права токена"
TOKEN_MODE=0640
if [ -n "$INSPECT" ] && [ "$INSPECT" != "[]" ]; then
  ENV_OUT="$(docker exec "$CONTAINER" bash -ic env 2>/dev/null || true)"
  # ROS сам выставляет часть переменных (ROS_LOCALHOST_ONLY=0), их получит и start_stack.sh:
  # ловим только то, что есть в интерактивной оболочке, но не в окружении после тех же source.
  BASE_OUT="$(docker exec "$CONTAINER" bash -c 'source /opt/ros/humble/setup.bash; [ -f /opt/ros/sensors/setup.bash ] && source /opt/ros/sensors/setup.bash; [ -f /home/fabian/ros2_ws/install/setup.bash ] && source /home/fabian/ros2_ws/install/setup.bash; env' 2>/dev/null || true)"
  DDS_VARS='^(ROS_DOMAIN_ID|RMW_IMPLEMENTATION|CYCLONEDDS_URI|ROS_LOCALHOST_ONLY|FASTRTPS_DEFAULT_PROFILES_FILE)='
  BAD="$(comm -23 <(grep -E "$DDS_VARS" <<<"$ENV_OUT" | sort || true) <(grep -E "$DDS_VARS" <<<"$BASE_OUT" | sort || true))"
  if [ -n "$BAD" ]; then
    fail "в контейнере заданы переменные DDS, start_stack.sh их не выставляет: $(tr '\n' ' ' <<<"$BAD")"
  else
    pass "в контейнере нет ROS_DOMAIN_ID/RMW/CYCLONEDDS/ROS_LOCALHOST_ONLY/FASTRTPS"
  fi
  HOST_UG="$(id -u "$SERVICE_USER" 2>/dev/null || echo ?):$(id -g "$SERVICE_USER" 2>/dev/null || echo ?)"
  CONT_UG="$(docker exec "$CONTAINER" id -u 2>/dev/null || echo ?):$(docker exec "$CONTAINER" id -g 2>/dev/null || echo ?)"
  if [ "$HOST_UG" = "$CONT_UG" ] && [ "$HOST_UG" != "?:?" ]; then
    pass "UID:GID $SERVICE_USER ($HOST_UG) = $CONTAINER_USER в контейнере, токен 0640"
  else
    TOKEN_MODE=0644
    pass "UID:GID $SERVICE_USER ($HOST_UG) != $CONTAINER_USER в контейнере ($CONT_UG), токен 0644"
  fi
fi

echo
if [ "${#FAILS[@]}" -gt 0 ]; then
  echo "Preflight: ${#FAILS[@]} проблем(ы), ничего не изменено."
  exit 1
fi
echo "Preflight пройден."
[ "$PREFLIGHT_ONLY" = 1 ] && exit 0

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: %s\n' "$*"
  else
    "$@"
  fi
}
write_if_changed() { # write_if_changed FILE CONTENT
  if [ -f "$1" ] && [ "$(cat "$1")" = "$2" ]; then
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY: write %s <- %s\n' "$1" "$2"
  else
    printf '%s\n' "$2" >"$1"
  fi
}

echo "== Установка"
OWNER="${SUDO_USER:-$(stat -c %U "$REPO_DIR")}"

run mkdir -p "$OPT_DIR"
if command -v rsync >/dev/null 2>&1; then
  run rsync -a --delete --exclude __pycache__ "$SRC_DIR/guide_launcher/" "$OPT_DIR/guide_launcher/"
  run rsync -a --delete "$SRC_DIR/web/" "$OPT_DIR/web/"
else
  run rm -rf "$OPT_DIR/guide_launcher" "$OPT_DIR/web"
  run cp -a "$SRC_DIR/guide_launcher" "$SRC_DIR/web" "$OPT_DIR/"
  run find "$OPT_DIR" -name __pycache__ -prune -exec rm -rf {} +
fi

run mkdir -p "$ETC_DIR/guide-launcher"
CONFIG="$ETC_DIR/guide-launcher/config.yaml"
if [ -f "$CONFIG" ]; then
  echo "  $CONFIG уже есть, не трогаю"
elif [ "$DRY_RUN" = 1 ]; then
  echo "DRY: создать $CONFIG из config.example.yaml (~/Desktop/Projects/Robo-guide -> $REPO_DIR)"
else
  sed "s#~/Desktop/Projects/Robo-guide#$REPO_DIR#g" "$SRC_DIR/config.example.yaml" >"$CONFIG"
  chmod 0644 "$CONFIG"
  echo "  создан $CONFIG (проверьте operator_pin, default_tour, rfid_secret_file)"
fi

TOKEN_DIR="$REPO_DIR/.guide_launcher"
run mkdir -p "$TOKEN_DIR"
run touch "$TOKEN_DIR/COLCON_IGNORE"
if [ -s "$TOKEN_DIR/bridge_token" ]; then
  echo "  bridge_token уже есть, не перезаписываю"
elif [ "$DRY_RUN" = 1 ]; then
  echo "DRY: сгенерировать $TOKEN_DIR/bridge_token"
else
  (umask 077 && head -c32 /dev/urandom | base64 | tr -d '=+/\n' >"$TOKEN_DIR/bridge_token")
  echo "  сгенерирован bridge_token"
fi
run chown -R "$OWNER:" "$TOKEN_DIR"
run chmod "$TOKEN_MODE" "$TOKEN_DIR/bridge_token"

run install -m 644 "$SRC_DIR/guide-launcher.service" "$UNIT_DIR/guide-launcher.service"
run mkdir -p "$UNIT_DIR/kiosk-hdmi.service.d"
run install -m 644 "$DISPLAY_DIR/kiosk-hdmi.service.d/10-guide-launcher.conf" \
  "$UNIT_DIR/kiosk-hdmi.service.d/10-guide-launcher.conf"

run install -m 755 "$DISPLAY_DIR/guide-kiosk-hdmi" "$BIN_DIR/guide-kiosk-hdmi"
write_if_changed "$ETC_DIR/guide-kiosk/url-hdmi" "$HDMI_URL"
write_if_changed "$ETC_DIR/guide-kiosk/url" "$FACE_URL"

run systemctl daemon-reload
run systemctl enable --now guide-launcher
run systemctl restart guide-launcher
run systemctl restart kiosk-hdmi

echo
echo "Готово. Диагностика: systemctl status guide-launcher; curl -s 127.0.0.1:$PORT/api/stack/status"
