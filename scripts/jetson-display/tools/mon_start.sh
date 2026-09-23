#!/bin/bash
# Наблюдение за донглом FL2000 и тачем. Ничего не меняет в системе кроме ftrace-настроек
# (восстанавливаются mon_stop.sh). Запускать от root.
set -eu
OUT=/tmp/mon
mkdir -p $OUT
T=/sys/kernel/debug/tracing
echo 0 > $T/tracing_on
echo nop > $T/current_tracer
echo > $T/set_event
echo mono > $T/trace_clock
cat $T/buffer_size_kb > $OUT/buffer_size_kb.orig
echo 20000 > $T/buffer_size_kb
echo > $T/trace
# URB: все bulk-кадры (>1 МБ), все interrupt-передачи (тач, прерывание донгла), все control (регистры донгла)
echo '(type == 2 && length > 1000000) || type == 3 || type == 0' > $T/events/xhci-hcd/xhci_urb_enqueue/filter
echo '(type == 2 && length > 1000000) || type == 3 || type == 0' > $T/events/xhci-hcd/xhci_urb_giveback/filter
echo 1 > $T/events/xhci-hcd/xhci_urb_enqueue/enable
echo 1 > $T/events/xhci-hcd/xhci_urb_giveback/enable
echo 1 > $T/events/xhci-hcd/xhci_handle_port_status/enable
echo 1 > $T/events/xhci-hcd/xhci_handle_cmd_reset_ep/enable
echo 1 > $T/events/xhci-hcd/xhci_handle_cmd_stop_ep/enable
echo 1 > $T/events/xhci-hcd/xhci_discover_or_reset_device/enable
echo 1 > $T/events/drm/drm_vblank_event/enable
echo 1 > $T/events/power/cpu_frequency/enable
echo 1 > $T/tracing_on
# trace_pipe -> файл (потоковое чтение, буфер не переполнится)
nohup cat $T/trace_pipe > $OUT/trace.txt 2>/dev/null &
echo $! > $OUT/pid.trace
# dmesg
nohup dmesg -w > $OUT/dmesg.txt 2>/dev/null &
echo $! > $OUT/pid.dmesg
# тач + CPU
nohup python3 $OUT/touchcpu.py > $OUT/touchcpu.txt 2>&1 &
echo $! > $OUT/pid.py
echo "started $(date -u +%T) mono=$(python3 -c 'import time;print(time.monotonic())')" | tee $OUT/started.txt
