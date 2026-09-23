#!/usr/bin/env bash
# Полный стек в контейнере: docker exec -d <container> <этот скрипт> (guide-launcher, start_cmd).
# Идемпотентен: если `ros2 launch` уже жив, выходит с 0 и не трогает stack_log.
# Работает из исходников после git pull, без colcon build самого скрипта.
WS=/home/fabian/ros2_ws
STACK_LOG="${STACK_LOG:-/tmp/stack.log}"

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

# stdout не tty: без этого python-ноды буферизуют вывод и лог в меню отстаёт.
export PYTHONUNBUFFERED=1
echo "start_stack: $(date -Is) ros2 launch guide_robot_bringup robot.launch.py $*"
exec ros2 launch guide_robot_bringup robot.launch.py "$@"
