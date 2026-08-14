# CLAUDE_CODE_TASK.md — Jetson-профиль для `iros_llm_server`

## Контекст платформы (проверено на железе 2026-08-14)

```
L4T          36.5.2  (JetPack 6.2.3)
CUDA         12.6.1.4
GPU          Orin, compute capability 8.7 (sm_87)
Power mode   MAXN_SUPER
Docker       nvidia runtime установлен, НО Default Runtime = runc
Память       7.4 GB total, unified (CPU+GPU общий пул)
```

## Что НЕ делать в этой задаче

Хостовые шаги делает человек, они не в репозитории:
`nvidia-ctk runtime configure`, `apt-mark hold`, `nvpmodel`, перепрошивка.
Не писать скрипты, которые их автоматизируют — не трогать системную конфигурацию.

Изменения в `guide_robot_llm` (деградация клиента, бюджет промпта) — отдельная задача,
здесь только `iros_llm_server`.

---

## Задача 1 — `scripts/cudamalloc_probe.sh` + `.cu`

**Приоритет: первый. Всё остальное заблокировано до его результата.**

Причина: на L4T 36.4.7 был сломан аллокатор крупных непрерывных CUDA-буферов
(NvMap). Мы обновились ради фикса, но **фикс не подтверждён замером**.

Написать:
- `scripts/cudamalloc_probe.cu` — принимает размер в GB, делает `cudaMalloc`,
  печатает `cudaGetErrorString`, освобождает, возвращает ненулевой код при ошибке.
- `scripts/cudamalloc_probe.sh` — компилирует через `/usr/local/cuda/bin/nvcc`,
  прогоняет 1/2/3 GB, печатает таблицу.

Требования: только stdlib + CUDA runtime, без зависимостей. Не запускать в контейнере —
проверяем платформу, а не образ.

**Приёмка:** 3 GB проходит. Если нет — остановиться и сообщить, задачи 2–6 не имеют смысла.

---

## Задача 2 — `scripts/mem_probe.sh`

Снимает реальный бюджет. Сейчас неизвестен: замеры дали 3.0 GB и 3.6 GB available
в разных условиях, непонятно, был ли поднят ROS-стек.

Печатает:
- `free -h`
- **`lfb` из `tegrastats`** — largest free block. Критично: `available` не гарантирует
  непрерывный блок нужного размера, а CUDA-буфер требует именно непрерывный.
  Парсить строку вида `RAM 1355/7620MB (lfb 14x4MB)`, вывести суммарный и максимальный блок.
- top-10 процессов по RSS

Флаг `--label <name>` для маркировки прогона. Гонять дважды: без ROS-стека и с ним.

Не использовать `nvidia-smi` — на Jetson его не существует.

---

## Задача 3 — `docker/Dockerfile.jetson`

Multi-stage. Upstream-образ `ghcr.io/ggml-org/llama.cpp:server-cuda` **не подходит**:
его arm64-вариант собран под SBSA, а Jetson — Tegra-стек (другая дистрибуция CUDA,
драйвер nvgpu). Плюс дефолтные CUDA-архитектуры ggml — 50/61/75/80/86/89/90/120a,
sm_87 отсутствует, и у Tegra нет forward-совместимости SASS.

```
ARG L4T_CUDA_TAG          # 12.6.x — СВЕРИТЬ существующие теги nvcr.io/nvidia/l4t-cuda
ARG LLAMA_REF             # ТОТ ЖЕ, что в x86-профиле
```

Build stage: `nvcr.io/nvidia/l4t-cuda:${L4T_CUDA_TAG}-devel`
```
-DGGML_CUDA=ON
-DCMAKE_CUDA_ARCHITECTURES=87     # обязательно, единственная арка
-DGGML_NATIVE=OFF
-DLLAMA_CURL=ON
-DCMAKE_BUILD_TYPE=Release
--target llama-server -j4          # НЕ $(nproc): nvcc уйдёт в OOM на 8 GB
```

Runtime stage: `nvcr.io/nvidia/l4t-cuda:${L4T_CUDA_TAG}-runtime` + `libgomp1 libcurl4 curl`.
`curl` нужен для healthcheck внутри сервиса — sidecar `llm-warmup` из x86-профиля
на Jetson не нужен.

Тег: `fabook/iros-llm:jetson-${LLAMA_REF}`.

Сборка нативно на Jetson. QEMU для CUDA-компиляции не работает.
Если упрётся в BuildKit + host-CUDA — `DOCKER_BUILDKIT=0`.

**`LLAMA_REF` обязан совпадать с x86-профилем.** Иначе расхождение в парсере GBNF
или семантике `cache_reuse` между ревизиями даст «на ноуте работает, на роботе нет».

**Приёмка:** в логе старта `ggml_cuda_init: found 1 CUDA devices:` и
`Device 0: Orin, compute capability 8.7`. Нет этой строки — сборка CPU-only, всё недействительно.

---

## Задача 4 — `docker-compose.jetson.yml` (оверрайд)

Дельта к базовому compose:

| Поле | Значение | Почему |
|---|---|---|
| `image` | `fabook/iros-llm:jetson-${LLAMA_REF}` | см. задачу 3 |
| `runtime: nvidia` | вместо `deploy.resources.reservations.devices` | на Jetson последний даёт битые маунты host-библиотек |
| `oom_score_adj: 500` | новое | **при OOM должен умирать LLM, а не nav2** |
| `healthcheck` | `curl -f localhost:8080/health` | curl теперь в образе |
| `mem_limit` | **не ставить** | cgroup-учёт nvgpu-аллокаций на Tegra ненадёжен |
| `llm-warmup` | убрать | прогрев внутрь entrypoint-обёртки |
| порт | `127.0.0.1` | потребитель — локальный ROS |

`oom_score_adj` — не косметика. На Tegra аллокация обычно проходит, а OOM-killer
срабатывает позже, когда разрастается KV-кэш. Без приоритета жертвой может стать
`slam_toolbox`. Диалог деградирует первым, навигация — последней.

---

## Задача 5 — `.env.jetson.example` + `config/models/jetson-*.env`

Дельта к x86:

| Параметр | x86 | Jetson | Почему |
|---|---|---|---|
| `CTX_SIZE` | 16384 | **2048** | KV линеен по контексту, память общая с ROS |
| `BATCH`/`UBATCH` | 2048/512 | **512/128** | compute-буферы префилла из того же пула |
| `--cache-type-k/v` | — | **q8_0** | ~2× экономии KV, требует flash_attn |
| `--mlock` | — | **не использовать** | повышает риск OOM для ROS |
| `N_GPU_LAYERS` | 999 | 999 | без изменений |
| `CACHE_REUSE` | 256 | 256 | без изменений |
| `NO_CONTEXT_SHIFT` | 1 | 1 | без изменений |

Профили моделей — **начать с 1.5B**, не с 3B:
`Qwen2.5-1.5B-Instruct-Q4_K_M`, `Qwen3-1.7B-Q4_K_M`.
3B (~1.9 GB весов) — верхняя планка, пробовать после того, как пайплайн заработает.
7–8B отменены: не влезают.

Добавить `--no-mmap` как параметр с дефолтом, снять обе ветки замером
(на Tegra веса копируются в cudaMalloc-буфер, а page cache от mmap мешает
непрерывной аллокации — гипотеза, подтверждать цифрами).

---

## Задача 6 — `systemd/iros-llm.service`

Дельта к x86-юниту:

1. `ExecStartPre`: `sync && echo 3 > /proc/sys/vm/drop_caches` — официальный воркэраунд
   NVIDIA, снижает фрагментацию перед аллокацией.
2. **LLM поднимается ПЕРВЫМ, до nav2 и slam_toolbox.** Аллокация на свежезагруженной
   системе проходит, на проработавшей часами — может не пройти. Это не противоречит
   порядку FSM супервизора (`…→NAV→LLM→OPERATIONAL`): FSM там *проверяет готовность*,
   физическая аллокация должна случиться раньше. Прописать `Before=` относительно
   ROS-юнитов.
3. `Restart=on-failure`, но в README отметить: перезапуск ≠ восстановление. После OOM
   на фрагментированной системе модель может не загрузиться повторно.

---

## Задача 7 — `docs/jetson_setup.md`

Только документация, без автоматизации:

- Хостовые шаги (что делает человек): `nvidia-ctk runtime configure --runtime=docker`,
  `"default-runtime": "nvidia"` в `daemon.json`, `apt-mark hold 'nvidia-l4t-*' nvidia-jetpack`.
- **`nvidia-smi` на Jetson не существует** — мониторинг через `tegrastats` / `jtop`.
  Проверить, что нигде в репозитории он не упоминается.
- Модели на NVMe, не на microSD.
- Swap не расширяет бюджет модели: загрузится, но инференс по свопнутым весам непригоден.
  Swap оправдан только на время сборки образа.
- L4T 36.5 заменил AppArmor на SELinux — snap-приложения ломаются. Отметить как известную
  особенность платформы.
- Занести подтверждённые версии: L4T 36.5.2, CUDA 12.6.1.4, sm_87.

---

## Порядок выполнения

1 → (стоп, проверка результата) → 2 → 3 → (проверка `compute capability 8.7`) → 4, 5, 6, 7

---

## Открытые вопросы, не для Claude Code

- Реальный бюджет памяти с полным ROS-стеком (даст задача 2).
- `CTX_SIZE 2048` ломает инъекцию полного корпуса экспонатов в системный промпт.
  Вероятно, потребуется выборка по `location_id` из `guide_robot_semantic_map`
  вместо полного корпуса. Считать бюджет промпта в токенах — отдельная задача.
- Решение «LLM на Jetson или на выносном сервере» остаётся открытым до цифр
  из задач 1, 2 и бенчмарка под нагрузкой ROS.