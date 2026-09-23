#!/bin/bash
OUT=/tmp/mon; T=/sys/kernel/debug/tracing
for p in trace dmesg py; do kill $(cat $OUT/pid.$p 2>/dev/null) 2>/dev/null || true; done
echo 0 > $T/tracing_on
echo > $T/set_event
for e in xhci_urb_enqueue xhci_urb_giveback; do echo 0 > $T/events/xhci-hcd/$e/filter; done
echo local > $T/trace_clock
cat $OUT/buffer_size_kb.orig > $T/buffer_size_kb 2>/dev/null || true
echo > $T/trace
echo "stopped $(date -u +%T)"; wc -l $OUT/trace.txt $OUT/touchcpu.txt $OUT/dmesg.txt
