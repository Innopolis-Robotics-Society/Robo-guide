# Настройка Jetson

Дельта к `docs/host_setup.md` (ноут-разработчика/сервер с дискретной NVIDIA GPU).
Ручные шаги хоста здесь **не автоматизированы намеренно** — системный конфиг
трогает человек, не скрипт (см. `iros_llm_server_JETSON_UPDATE.md`, «Что НЕ
делать в этой задаче»).

## Подтверждённые версии (замер 2026-08-14)

```
L4T          36.5.2  (JetPack 6.2.3)
CUDA         12.6.1.4
GPU          Orin, compute capability 8.7 (sm_87)
Power mode   MAXN_SUPER
Docker       nvidia runtime установлен, Default Runtime = runc
Память       7.4 GB total, unified (CPU+GPU общий пул)
```

Перед деплоем на другой ревизии L4T/JetPack — перепроверить эти значения и
обновить таблицу выше. `sm_87` жёстко зашит в `docker/Dockerfile.jetson`
(`CMAKE_CUDA_ARCHITECTURES=87`) — Tegra не имеет forward-совместимости SASS
с другими архитектурами, смена GPU требует правки этого значения.

## 0. Порядок: сначала задача 1

**Прежде чем что-либо из нижеперечисленного имеет смысл** — прогнать
`scripts/cudamalloc_probe.sh` на этом хосте и убедиться, что 3 GB выделяется
успешно. L4T 36.4.7 имел сломанный аллокатор крупных непрерывных CUDA-буферов
(NvMap); обновление до 36.5.2 должно чинить это, но **не подтверждено
замером** на конкретной единице железа, пока скрипт не прогнан руками.

```bash
cd llm_server
./scripts/cudamalloc_probe.sh
```

Если 3 GB не проходит — остановиться, дальнейшие шаги не имеют смысла.

## 1. nvidia-container-toolkit (хост, вручную)

```bash
sudo nvidia-ctk runtime configure --runtime=docker
```

Проверить, что в `/etc/docker/daemon.json` появилось `"default-runtime": "nvidia"`
(или полагаться на `runtime: nvidia`, явно заданный в
`docker-compose.jetson.yml`, — второе надёжнее и не зависит от дефолта демона).
Перезапустить Docker: `sudo systemctl restart docker`.

## 2. Заморозка версий пакетов (хост, вручную)

```bash
sudo apt-mark hold 'nvidia-l4t-*' nvidia-jetpack
```

Автообновление L4T/JetPack способно сломать связку CUDA-версия ↔
`nvcr.io/nvidia/l4t-cuda:${L4T_CUDA_TAG}` в `docker/Dockerfile.jetson` — тег
образа привязан к конкретной версии CUDA, molчаливый апгрейд хоста может
разойтись с образом.

## 3. Мониторинг: tegrastats/jtop, НЕ nvidia-smi

**`nvidia-smi` на Jetson не существует** — Tegra использует единый драйвер
`nvgpu`, не дискретный NVIDIA-драйвер с обычным user-space стеком. Проверено:
единственное упоминание `nvidia-smi` в этом репозитории вне
`iros_llm_server_JETSON_UPDATE.md`/`mem_probe.sh` — это `docs/host_setup.md`
(команда для ноута/сервера с дискретной GPU, там `nvidia-smi` реально есть;
Jetson-профиль эту команду не использует нигде).

```bash
tegrastats                 # потоковый вывод RAM/GPU/CPU
sudo pip3 install jetson-stats && jtop   # интерактивный TUI поверх tegrastats
```

`scripts/mem_probe.sh` парсит одну строку `tegrastats` (largest free block).

## 4. Модели на NVMe, не на microSD

`MODELS_DIR` (`.env.jetson.example`) должен указывать на NVMe-том. microSD
на Jetson на порядок медленнее по случайному чтению — при первом запуске
`llama-server` мапит веса в память последовательно, но сам факт хранения
модели на microSD увеличивает время холодного старта контейнера и риск
таймаута `WARMUP_TIMEOUT_S` при повторных перезапусках systemd-юнита.

## 5. Swap не расширяет бюджет модели

Своп на Jetson **не** увеличивает эффективный объём, доступный под модель:
инференс по свопнутым весам (или KV-кэшу) непригоден по латентности — своп
даёт "загрузится", а не "будет работать". Своп оправдан ровно на время
**сборки** образа (`docker build` в `docker/Dockerfile.jetson`, компиляция
`llama.cpp`/`nvcc` может требовать больше памяти, чем инференс), не на
время работы сервиса. Не полагаться на своп как способ обойти `CTX_SIZE`/
`BATCH` из `.env.jetson.example` — это бюджет, посчитанный под реальный
объём, не под объём+своп.

## 6. L4T 36.5: AppArmor заменён на SELinux

Известная особенность платформы, не специфична для `iros-llm`, но может
удивить при отладке контейнеров: L4T 36.5 переключил security-модуль с
AppArmor на SELinux. Snap-приложения на хосте могут ломаться (не
затрагивает `iros-llm` — тот работает через Docker, не snap), но при
диагностике странных `Permission denied` внутри контейнера — проверить
`sudo ausearch -m avc -ts recent` прежде чем списывать на баг образа.

## 7. Проверка готовности

```bash
curl -s localhost:8080/health
curl -s localhost:8080/props    # model_path, ctx_size -- сверить с .env
```

Дополнительно на Jetson — сверить лог старта контейнера:

```bash
docker compose -f docker-compose.yml -f docker-compose.jetson.yml logs llm | grep -E 'ggml_cuda_init|compute capability'
```

Ожидаемо: `ggml_cuda_init: found 1 CUDA devices:` и `Device 0: Orin, compute
capability 8.7`. Отсутствие этих строк означает CPU-only сборку — образ
собран неверно, см. `docker/Dockerfile.jetson`.

## 8. Systemd, перезапуск ≠ восстановление

`systemd/iros-llm-jetson.service` — Jetson-вариант юнита (drop_caches перед
стартом, поднимается до остального ROS-стека, `Restart=on-failure`). Важная
оговорка: на Tegra успешная аллокация памяти под модель зависит от текущей
фрагментации unified-памяти, которая накапливается со временем работы
системы. `Restart=on-failure` перезапустит контейнер после падения (OOM,
неудачный `cudaMalloc`), но **не гарантирует**, что повторная попытка
пройдёт успешнее — если причина в фрагментации, а не во временном сбое,
процесс будет падать в рестарт-лупе, пока не поможет `ExecStartPre`
(`drop_caches`) или перезагрузка хоста. Проверять `journalctl -u
iros-llm-jetson.service` при подозрении на такой луп, не полагаться на
`Restart=` как единственную защиту.

Установка — см. `docs/host_setup.md` §6, с заменой имени юнита:

```bash
sudo mkdir -p /opt/iros-llm-server
sudo rsync -a --exclude models ./ /opt/iros-llm-server/
sudo cp systemd/iros-llm-jetson.service /etc/systemd/system/
# поправить WorkingDirectory/-f и <ros-stack>.service в юните под фактический деплой
sudo systemctl daemon-reload
sudo systemctl enable --now iros-llm-jetson.service
```

## Открытые вопросы (не решены в рамках этой задачи)

См. `iros_llm_server_JETSON_UPDATE.md`, раздел «Открытые вопросы, не для
Claude Code»: реальный бюджет памяти с полным ROS-стеком (задача 2),
бюджет промпта в токенах при `CTX_SIZE=2048` (вероятно потребуется выборка
по `location_id` вместо полного корпуса экспонатов), решение
«LLM на Jetson или на выносном сервере».
