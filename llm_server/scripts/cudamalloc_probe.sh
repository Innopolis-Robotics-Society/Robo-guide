#!/usr/bin/env bash
# Компилирует и прогоняет cudamalloc_probe.cu на 1/2/3 GB, печатает таблицу.
#
# Задача 1 из iros_llm_server_JETSON_UPDATE.md: подтвердить замером, что
# L4T 36.5.2 действительно чинит аллокатор крупных непрерывных CUDA-буферов
# (был сломан на 36.4.7, см. комментарий в .cu). Запускать НА ХОСТЕ Jetson,
# не в контейнере -- проверяем платформу (драйвер + NvMap), а не образ.
#
# Приёмка: 3 GB должно пройти. Если нет -- остальные задачи профиля
# (2-6) не имеют смысла, см. JETSON_UPDATE.md.
set -euo pipefail

NVCC="${NVCC:-/usr/local/cuda/bin/nvcc}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${SCRIPT_DIR}/cudamalloc_probe.cu"
BIN="$(mktemp -t cudamalloc_probe.XXXXXX)"

cleanup() { rm -f "$BIN"; }
trap cleanup EXIT

if [ ! -x "$NVCC" ]; then
  echo "error: nvcc не найден по '${NVCC}' (проверьте CUDA toolkit, или задайте NVCC=...)" >&2
  exit 1
fi

echo "==> компиляция ${SRC}" >&2
"$NVCC" -O2 -o "$BIN" "$SRC"

sizes=(1 2 3)
declare -A results
overall_rc=0

printf '\n%-10s %-8s %s\n' "size_gb" "result" "detail"
printf '%-10s %-8s %s\n' "-------" "------" "------"

for gb in "${sizes[@]}"; do
  out="$("$BIN" "$gb" 2>&1)" && rc=0 || rc=$?
  detail="${out#*: }"
  status="${out%%:*}"
  if [ "$rc" -eq 0 ]; then
    printf '%-10s %-8s %s\n' "$gb" "OK" "$detail"
  else
    printf '%-10s %-8s %s\n' "$gb" "FAIL" "$detail"
    overall_rc=1
  fi
  results["$gb"]=$rc
done

echo

if [ "${results[3]}" -ne 0 ]; then
  echo "СТОП: 3 GB не прошёл. Задачи 2-6 профиля Jetson не имеют смысла," \
       "пока это не исправлено (см. JETSON_UPDATE.md, задача 1)." >&2
  exit 1
fi

echo "3 GB прошёл -- аллокатор крупных непрерывных буферов работает на этой прошивке." >&2
exit "$overall_rc"
