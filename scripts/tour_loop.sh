#!/usr/bin/env bash
# Гоняет экскурсию по кругу до остановки -- оценка времени работы от батареи.
# Запуск в контейнере при поднятом стеке:
#   docker exec -d robo-guide-jetson-1 /home/fabian/ros2_ws/src/scripts/tour_loop.sh expo_one
#   tour_loop.sh <tour_id> [флаги mission_cli tour, по умолчанию --no-confirm --no-return-home]
# Остановка: kill <pid скрипта> -- текущий тур отменяется. Лог (одна строка на круг, fsync):
#   ~/ros2_ws/log/tour_loop.log в контейнере = <repo>/log/tour_loop.log на хосте.
TOUR="${1:?tour_id, например expo_one}"
shift
FLAGS=("$@")
[ ${#FLAGS[@]} -eq 0 ] && FLAGS=(--no-confirm --no-return-home)
LOG="${TOUR_LOOP_LOG:-/home/fabian/ros2_ws/log/tour_loop.log}"
RETRY_S="${TOUR_LOOP_RETRY_S:-15}"

source /opt/ros/humble/setup.bash
source /home/fabian/ros2_ws/install/setup.bash
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-1}"  # как у стека, start_stack.sh
# Без job control фоновый child получает SIGINT=SIG_IGN, и отмена тура по kill не дойдёт.
set -m

log() {
  echo "$(date -Is) $*" >>"$LOG"
  sync "$LOG"
}

child=""
stop() {
  [ -n "$child" ] && kill -INT -- "-$child" 2>/dev/null && wait "$child"
  log "stop: laps=$lap ok=$ok elapsed_min=$(( ($(date +%s) - t0) / 60 ))"
  exit 0
}
trap stop INT TERM

t0=$(date +%s)
lap=0
ok=0
log "start: pid=$$ tour=$TOUR flags=${FLAGS[*]}"
while true; do
  lap=$((lap + 1))
  ts=$(date +%s)
  out=$(mktemp)
  ros2 run guide_robot_mission_control mission_cli tour --tour "$TOUR" "${FLAGS[@]}" >"$out" 2>&1 &
  child=$!
  wait "$child"
  rc=$?
  child=""
  res=$(grep -m1 '^outcome=' "$out" || tail -n1 "$out")
  rm -f "$out"
  [[ "$res" == outcome=COMPLETED* ]] && ok=$((ok + 1))
  log "lap=$lap rc=$rc dur_s=$(( $(date +%s) - ts )) total_min=$(( ($(date +%s) - t0) / 60 )) ok=$ok $res"
  [[ "$res" == outcome=COMPLETED* ]] || sleep "$RETRY_S"
done
