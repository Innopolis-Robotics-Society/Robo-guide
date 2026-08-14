#!/usr/bin/env bash
# Снимает реальный бюджет памяти на Jetson: free -h, largest-free-block из
# tegrastats и top-10 процессов по RSS.
#
# Зачем lfb отдельно от free/available: CUDA-буфер под KV-кэш и веса модели
# требует НЕПРЕРЫВНОГО блока, а `available` в free(1) считает сумму
# фрагментов. На unified-памяти Jetson (CPU+GPU общий пул) фрагментация от
# долго работающего ROS-стека -- реальный риск, см. JETSON_UPDATE.md, задача 2.
#
# Гонять дважды: один раз до старта ROS-стека, один раз после (--label
# отличает прогоны в выводе).
#
# Не использовать nvidia-smi -- на Jetson его не существует.
set -euo pipefail

label=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --label)
      label="${2:-}"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--label <name>]" >&2
      exit 0
      ;;
    *)
      echo "error: неизвестный аргумент '$1'" >&2
      exit 1
      ;;
  esac
done

timestamp="$(date -Iseconds)"
echo "=== mem_probe: label='${label}' at ${timestamp} ==="
echo

echo "--- free -h ---"
free -h
echo

echo "--- tegrastats: largest free block (lfb) ---"
if ! command -v tegrastats >/dev/null 2>&1; then
  echo "error: tegrastats не найден -- этот скрипт предназначен для запуска на Jetson" >&2
  exit 1
fi

# tegrastats -- потоковая утилита; берём одну строку и останавливаем.
# --interval в мс, 1000 достаточно для одного снимка.
tegrastats_line="$(timeout 3 tegrastats --interval 1000 | head -n 1 || true)"

if [ -z "$tegrastats_line" ]; then
  echo "error: не удалось получить строку от tegrastats (таймаут)" >&2
  exit 1
fi

echo "raw: ${tegrastats_line}"

# Формат: "RAM 1355/7620MB (lfb 14x4MB)" -- lfb NxSIZE: N свободных блоков
# размера SIZE каждый в верхнем бакете гистограммы NvMap. Гарантированно
# непрерывен только SIZE (один блок), не N*SIZE -- это НЕ один непрерывный
# кусок, а count блоков такого размера.
if [[ "$tegrastats_line" =~ lfb[[:space:]]+([0-9]+)x([0-9]+)MB ]]; then
  lfb_count="${BASH_REMATCH[1]}"
  lfb_block_mb="${BASH_REMATCH[2]}"
  lfb_total_mb=$((lfb_count * lfb_block_mb))
  echo "largest contiguous block (гарантировано): ${lfb_block_mb} MB"
  echo "блоков такого размера в верхнем бакете: ${lfb_count} (сумма ${lfb_total_mb} MB, НЕ непрерывно)"
else
  echo "error: не удалось распарсить 'lfb NxSIZEMB' из строки tegrastats" >&2
  exit 1
fi
echo

echo "--- top-10 процессов по RSS ---"
ps -eo pid,comm,rss --sort=-rss | head -n 11
echo

echo "=== конец прогона label='${label}' ==="
