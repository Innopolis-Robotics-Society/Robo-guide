#!/usr/bin/env bash
# Runtime-зависимости API, поиска и голоса. Отдельный слой ПОСЛЕ ML:
# изменения здесь не требуют пересборки torch на Jetson. Повторяем общие
# зависимости из ранних слоёв, чтобы проверить ограничения ПОСЛЕ установки ML
# и поддержать обновление ранее собранных образов этим скриптом.
#
# Образ НЕ делает rosdep по src/: package.xml (python3-aiohttp) на
# контейнер не действует, пока пакеты не в этом скрипте / 30-python-common.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
    "numpy<2" \
    aiohttp \
    rank_bm25 \
    requests \
    scipy \
    scikit-learn \
    tqdm \
    soxr \
    sherpa-onnx \
    "setuptools<80" \
    "pytest==8.4.2"

# Wakeword использует выбранный платформой ONNX Runtime. Не подменять
# onnxruntime-gpu CPU-пакетом и не подтягивать tflite-runtime на Jetson.
python3 -m pip install --no-cache-dir --no-deps "openwakeword==0.6.0"
