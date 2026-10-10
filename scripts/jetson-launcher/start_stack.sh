#!/usr/bin/env bash
# Полный стек в контейнере: docker exec -d <container> <этот скрипт> (guide-launcher, start_cmd).
# Идемпотентен: если `ros2 launch` уже жив, выходит с 0 и не трогает stack_log.
# Работает из исходников после git pull, без colcon build самого скрипта.
WS=/home/fabian/ros2_ws
STACK_LOG="${STACK_LOG:-/tmp/stack.log}"
# Внешний USB-хаб с лидарами и микрофоном; пусто -- не сбрасывать (другой робот/раскладка).
HUB="${STACK_USB_HUB-1-2.1}"
HUB_SETTLE_S="${STACK_USB_SETTLE_S:-10}"

if pgrep -f "ros2 launch" >/dev/null; then
  exit 0
fi

# С этой строки всё, включая ошибки source, попадает в лог (перезапись).
exec >"$STACK_LOG" 2>&1

cd "$WS" || exit 1

# docker exec не читает ~/.bashrc: окружение ROS задаём явно.
source /opt/ros/humble/setup.bash
[ -f /opt/ros/sensors/setup.bash ] && source /opt/ros/sensors/setup.bash
if [ ! -f "$WS/install/setup.bash" ]; then
  echo "start_stack: $WS/install/setup.bash не найден -- нужен colcon build"
  exit 1
fi
source "$WS/install/setup.bash"

# Два лидара за одним single-TT хабом виснут (-110, sllidar code 80008004) на open/close, а
# usb_serial_preflight как раз открывает и закрывает порты. Поэтому: сброс хаба целиком, пауза на
# переподключение и старт без preflight (README bringup, «Зависание single-TT USB-хаба»).
if [ -n "$HUB" ] && [ -d "/sys/bus/usb/devices/$HUB" ]; then
  echo "start_stack: сброс USB-хаба $HUB, пауза ${HUB_SETTLE_S} с"
  python3 "$WS/src/guide_robot_bringup/guide_robot_bringup/usb_serial_preflight.py" \
    --reset-usb "$HUB" || echo "start_stack: сброс хаба не удался, продолжаю"
  sleep "$HUB_SETTLE_S"
  PREFLIGHT_ARG=usb_preflight:=false
fi

# stdout не tty: без этого python-ноды буферизуют вывод и лог в меню отстаёт.
export PYTHONUNBUFFERED=1
echo "start_stack: $(date -Is) ros2 launch guide_robot_bringup robot.launch.py $*"
exec ros2 launch guide_robot_bringup robot.launch.py ${PREFLIGHT_ARG:+"$PREFLIGHT_ARG"} "$@"
