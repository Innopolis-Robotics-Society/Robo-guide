# DDS-окружение стека робота. source из start_stack.sh, tour_loop.sh и ~/.bashrc контейнера,
# чтобы CLI видел тот же DDS, что и стек.
#   по умолчанию   -- CycloneDDS только на loopback (cyclonedds_local.xml);
#   STACK_RMW=rmw_fastrtps_cpp -- прежний FastDDS (тоже без сети);
#   STACK_DDS_LAN=1 -- DDS в Wi-Fi (rviz с ноутбука).
# Нет rmw_cyclonedds_cpp в образе -- откат на FastDDS, чтобы стек всё равно поднялся.
_ros_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RMW_IMPLEMENTATION="${STACK_RMW-rmw_cyclonedds_cpp}"
if [ "$RMW_IMPLEMENTATION" = rmw_cyclonedds_cpp ] \
  && [ ! -f "/opt/ros/${ROS_DISTRO:-humble}/lib/librmw_cyclonedds_cpp.so" ]; then
  echo "ros_env: rmw_cyclonedds_cpp не установлен -- FastDDS" >&2
  RMW_IMPLEMENTATION=rmw_fastrtps_cpp
fi
export RMW_IMPLEMENTATION
unset CYCLONEDDS_URI
if [ "${STACK_DDS_LAN:-0}" = 1 ]; then
  export ROS_LOCALHOST_ONLY=0
elif [ "$RMW_IMPLEMENTATION" = rmw_cyclonedds_cpp ]; then
  # Интерфейс задаёт XML; ROS_LOCALHOST_ONLY=1 у Cyclone поверх него не нужен.
  export ROS_LOCALHOST_ONLY=0
  export CYCLONEDDS_URI="file://$_ros_env_dir/cyclonedds_local.xml"
else
  export ROS_LOCALHOST_ONLY=1
fi
unset _ros_env_dir
