#!/usr/bin/env bash
# Гоняет экскурсию по кругу до остановки -- оценка времени работы от батареи.
# Запуск в контейнере при поднятом стеке:
#   docker exec -d robo-guide-jetson-1 /home/fabian/ros2_ws/src/scripts/tour_loop.sh expo_one
#   tour_loop.sh <tour_id> [флаги mission_cli tour, по умолчанию --no-confirm --no-return-home]
# Остановка: kill <pid скрипта> -- текущий тур отменяется; «Стоп» тура в UI тоже завершает цикл. Лог (одна строка на круг, fsync):
#   ~/ros2_ws/log/tour_loop.log в контейнере = <repo>/log/tour_loop.log на хосте.
TOUR="${1:?tour_id, например expo_one}"
shift
FLAGS=("$@")
[ ${#FLAGS[@]} -eq 0 ] && FLAGS=(--no-confirm --no-return-home)
LOG="${TOUR_LOOP_LOG:-/home/fabian/ros2_ws/log/tour_loop.log}"
RETRY_S="${TOUR_LOOP_RETRY_S:-15}"
# Под нагрузкой discovery нового процесса идёт ~10 с, дефолтные 5 с mission_cli не хватает.
CALL_TIMEOUT_S="${TOUR_LOOP_TIMEOUT_S:-30}"

source /opt/ros/humble/setup.bash
source /home/fabian/ros2_ws/install/setup.bash
source /home/fabian/ros2_ws/src/scripts/jetson-launcher/ros_env.sh  # тот же DDS, что у стека
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
  ros2 run guide_robot_mission_control mission_cli --timeout-s "$CALL_TIMEOUT_S" tour --tour "$TOUR" "${FLAGS[@]}" >"$out" 2>&1 &
  child=$!
  wait "$child"
  rc=$?
  child=""
  res=$(grep -m1 -E '^(outcome=|ошибка)' "$out" || tail -n1 "$out")
  rm -f "$out"
  [[ "$res" == outcome=COMPLETED* ]] && ok=$((ok + 1))
  log "lap=$lap rc=$rc dur_s=$(( $(date +%s) - ts )) total_min=$(( ($(date +%s) - t0) / 60 )) ok=$ok $res"
  # CANCELED = кто-то нажал «Стоп» (UI/голос) -- человек рядом хочет остановить робота, не перезапускаем.
  if [[ "$res" == outcome=CANCELED* ]]; then
    log "stop: tour canceled by operator, laps=$lap ok=$ok elapsed_min=$(( ($(date +%s) - t0) / 60 ))"
    exit 0
  fi
  [[ "$res" == outcome=COMPLETED* ]] || sleep "$RETRY_S"
done
