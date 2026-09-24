#!/usr/bin/env bash
# Скачать fp32 GigaAM v3 CTC для asr_backend=onnxruntime (GPU) в guide_robot_voice/models/.
# В git не лежит (885 МБ); после загрузки -- colcon build guide_robot_voice.
#
#   scripts/fetch_asr_model.sh
set -euo pipefail

URL="https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx/resolve/main/gigaam_v3_ctc.onnx"
SIZE=885264480
DEST="$(cd "$(dirname "$0")/.." && pwd)/guide_robot_voice/models/gigaam_v3_ctc.onnx"

if [[ -f "$DEST" && "$(stat -c %s "$DEST")" == "$SIZE" ]]; then
  echo "уже есть: $DEST"
  exit 0
fi
curl -fL --retry 3 -o "$DEST.part" "$URL"
[[ "$(stat -c %s "$DEST.part")" == "$SIZE" ]] || { echo "размер не совпал: $DEST.part" >&2; exit 1; }
mv "$DEST.part" "$DEST"
echo "готово: $DEST"
