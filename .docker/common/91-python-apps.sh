#!/usr/bin/env bash
# Pip-зависимости воркспейса (не ML). Отдельный слой ПОСЛЕ 90, чтобы
# докинуть aiohttp/rank_bm25 в уже собранный fabook/iros:jetson без
# пересборки 30-python-common → 40-ml-jetson (torch).
#
# Образ НЕ делает rosdep по src/: package.xml (python3-aiohttp) на
# контейнер не действует, пока пакеты не в этом скрипте / 30-python-common.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
    aiohttp \
    rank_bm25
