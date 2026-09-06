# `iros_llm_server` — спецификация

Контейнеризованный inference-бэкенд для `guide_robot_llm`. Живёт **вне** ROS-монорепо
(отдельный репозиторий или `tools/llm_server/` — на усмотрение), т.к. не является ROS-пакетом
и не собирается через `colcon`.

## 0. Границы модуля

**Входит:**
- Docker Compose поверх upstream-образа `llama.cpp`
- Управление GGUF-моделями (скачивание, хранение вне образа)
- Прогрев (warmup) и readiness-контракт
- Скрипты сетевой публикации, замеров и обслуживания хоста

**Не входит (осознанно):**
- Failover между бэкендами — живёт в `guide_robot_llm/llm_client`, там уже есть список
  бэкендов и circuit breaker. Сервер тупой: один контейнер = один бэкенд.
- GBNF-грамматики — хранятся в `guide_robot_llm`, передаются per-request в теле.
  Флаг `--grammar-file` использовать **нельзя**: он глобальный, а свободная речь
  (`Say` в dialog scope) идёт без грамматики.
- Chat-шаблоны и tool-calling сервера (`--jinja` + нативный tools API) — не используем,
  ReAct-цикл рукописный, промпт собирается клиентом.

**Контракт наружу:** `http://<host>:<port>/v1` (OpenAI-compatible) + `GET /health` + `GET /metrics`.

## 1. Дерево файлов

```
iros_llm_server/
├── README.md
├── .env.example
├── .gitignore                 # models/, .env
├── docker-compose.yml
├── config/
│   ├── system_prompt.txt      # тот же текст, что шлёт llm_client (для прогрева префикса)
│   └── models/
│       ├── qwen7b-q4.env      # профили: MODEL_FILE + тюнинг
│       └── cpu-fallback.env
├── scripts/
│   ├── fetch_model.sh
│   ├── warmup.sh
│   ├── bench_ttft.py
│   └── publish_mdns.sh
├── systemd/
│   └── iros-llm.service
└── docs/
    └── host_setup.md
```

Никакого `Dockerfile`. Upstream-образ используется как есть, вся конфигурация — через
`LLAMA_ARG_*` env. Это убирает пересборку при смене модели/параметров.

> Исключение на будущее: если понадобится offline-деплой в музей без volume —
> добавить `docker/Dockerfile.baked`, который `COPY` GGUF внутрь. Сейчас не делать.

## 2. `docker-compose.yml`

### Сервис `llm`

| Поле | Значение |
|---|---|
| `image` | `ghcr.io/ggml-org/llama.cpp:${LLAMA_TAG}` — **тег обязателен**, без дефолта (`:?`) |
| `restart` | `unless-stopped` |
| `ports` | `"${LLM_BIND:-127.0.0.1}:${LLM_PORT:-8080}:8080"` |
| `volumes` | `${MODELS_DIR:-./models}:/models:ro` |
| `deploy.resources.reservations.devices` | `driver: nvidia`, `count: all`, `capabilities: [gpu]` |
| `logging` | `json-file`, `max-size: 20m`, `max-file: 5` |

`LLM_BIND` — единственный переключатель между профилями:
`127.0.0.1` = симулятор (ROS на том же ноуте), `0.0.0.0` = Jetson по Wi-Fi.
Внутри контейнера `LLAMA_ARG_HOST` всегда `0.0.0.0`.

### Env-переменные сервиса `llm`

```
LLAMA_ARG_MODEL             /models/${MODEL_FILE}
LLAMA_ARG_ALIAS             ${MODEL_ALIAS}
LLAMA_ARG_HOST              0.0.0.0
LLAMA_ARG_PORT              8080
LLAMA_ARG_CTX_SIZE          ${CTX_SIZE:-16384}
LLAMA_ARG_N_GPU_LAYERS      ${N_GPU_LAYERS:-999}
LLAMA_ARG_N_PARALLEL        ${N_PARALLEL:-1}
LLAMA_ARG_BATCH             ${BATCH:-2048}
LLAMA_ARG_UBATCH            ${UBATCH:-512}
LLAMA_ARG_FLASH_ATTN        ${FLASH_ATTN:-on}
LLAMA_ARG_CACHE_REUSE       ${CACHE_REUSE:-256}
LLAMA_ARG_NO_CONTEXT_SHIFT  1
LLAMA_ARG_ENDPOINT_METRICS  1
LLAMA_API_KEY               ${LLM_API_KEY:-}
```

Обоснование неочевидных:

- **`N_PARALLEL=1`.** `CTX_SIZE` делится между слотами. ReAct-цикл строго
  последовательный → второй слот только урезал бы контекст. Поднимать только при
  появлении второго потребителя.
- **`CACHE_REUSE`.** Префикс (system prompt + определения инструментов) переиспользуется
  между ходами, префилится только хвост. **Требует от `llm_client`**: волатильное
  (`MissionState`, история) — строго ПОСЛЕ статики. Если порядок нарушен, кэш не работает
  и TTFT растёт на полный префилл каждый ход.
- **`NO_CONTEXT_SHIFT=1`.** Нужен явный отказ, а не молчаливый сдвиг окна. История
  принадлежит клиенту (barge-in обрезает контекст до фактически произнесённого текста);
  серверный сдвиг рассинхронизировал бы это. Переполнение должно быть видимой ошибкой.

> **Проверить на своей сборке:** имена `LLAMA_ARG_*` дрейфуют между релизами. Перед
> первым запуском — `docker run --rm --entrypoint /app/llama-server <image> --help`
> и сверить. Если какое-то имя не поддерживается, откатить его в явный `command:`.

### Сервис `llm-warmup`

One-shot sidecar, `image: curlimages/curl:${CURL_TAG}`, `entrypoint: ["/bin/sh", "/warmup.sh"]`,
`depends_on: [llm]`, `restart: "no"`. Ходит на `http://llm:8080` по внутренней сети compose.
Монтирует `scripts/warmup.sh` и `config/system_prompt.txt` read-only.

Отдельный контейнер, а не healthcheck внутри `llm`: upstream-образ slim, наличие `curl`
и шелла внутри него не гарантировано — не завязываемся на его содержимое.

## 3. Скрипты

### `scripts/warmup.sh` (POSIX sh)

1. Поллить `GET /health` (интервал 2 с, лимит `WARMUP_TIMEOUT_S`, дефолт 600).
   Загрузка модели с диска в VRAM на холодную занимает десятки секунд.
2. По готовности — `POST /v1/chat/completions`, тело: system-сообщение из
   `/system_prompt.txt` + короткий user-стимул, `max_tokens: 1`, `stream: false`.
   Цель — не сгенерировать ответ, а положить KV статического префикса в слот.
3. Заголовок `Authorization: Bearer $LLM_API_KEY` только если переменная непустая.
4. Exit 0 при успехе, exit 1 по таймауту (видно в `docker compose ps`).

### `scripts/fetch_model.sh`

`curl` по HF `resolve/main` URL, без `huggingface-cli` (лишняя зависимость).
- Аргументы: `<repo_id> <filename> [dest_dir]`, dest по умолчанию `./models`.
- `-L --fail --continue-at -` (докачка), `Authorization: Bearer $HF_TOKEN` если задан.
- Идемпотентность: если файл существует и размер совпал с `Content-Length` из `HEAD` — skip.
- Проверять свободное место перед стартом.

### `scripts/bench_ttft.py`

Только stdlib (`urllib`, `json`, `time`, `argparse`, `statistics`). Ручной разбор SSE.

Меряет по N итераций: **TTFT** (до первого непустого `content` delta), **decode tok/s**,
**total**. Печатает p50/p95. Флаги: `--url`, `--n`, `--prompt-file`, `--max-tokens`, `--api-key`.

Два обязательных прогона, результаты в `README.md`:
- localhost (baseline),
- с Jetson через Wi-Fi (реальная цифра для L2-бюджета в трёхуровневой модели задержки).

Первую итерацию отбрасывать (холодный кэш).

### `scripts/publish_mdns.sh`

`avahi-publish -a -R iros-llm.local <ip>`, IP берётся с интерфейса дефолтного маршрута.
Стабильное имя, не зависящее от хостнейма `mook-ROG-Zephyrus-G16`.

**Пометить в README как опциональное удобство для разработки.** Основной путь адресации —
DHCP-резервация по MAC на роутере + явный IP в ROS-параметре. Причина: mDNS-резолвинг
из Docker-контейнера на Jetson требует `libnss-mdns` и проброса host-DNS, работает
не везде и молча деградирует.

## 4. `systemd/iros-llm.service`

`Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=/usr/bin/docker compose -f <path> up -d`,
`ExecStop=... down`, `After=docker.service network-online.target`, `WantedBy=multi-user.target`.

## 5. `docs/host_setup.md`

Ручные шаги, скриптом не автоматизировать (модификация системного конфига):

1. **nvidia-container-toolkit** — проверка: `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi`.
2. **Файрвол:** `sudo ufw allow from <jetson_ip> to any port <LLM_PORT> proto tcp`.
   Ограничить конкретным IP, не открывать порт в сеть.
3. **Запрет сна ноутбука:** `HandleLidSwitch=ignore` в `/etc/systemd/logind.conf`;
   `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
   Иначе сервер отваливается посреди демо при закрытии крышки.
4. **AP isolation.** На музейном/гостевом Wi-Fi клиенты часто изолированы на L2 —
   формально «одна сеть», но Jetson не видит ноут. Проверять `ping` до деплоя;
   fallback — свой роутер или Ethernet.
5. **Проверка готовности:** `curl -s localhost:<port>/health` и `/props` (отдаёт модель и ctx).

## 6. Интеграция с ROS-стороной

- `guide_robot_llm` параметр `llm.base_url` → `http://<ip>:<port>/v1`. Уже параметр, не константа.
- Стадия LLM в FSM супервизора (`SAFETY→DRIVE→WAIT_TF→NAV→**LLM**→OPERATIONAL`) делает
  реальный `GET /health` с коротким connect-таймаутом, а не просто проверяет наличие параметра.
- Таймауты в клиенте раздельные: connect 1–2 с (сеть локальная), read — длинный
  (генерация идёт секундами). Один общий таймаут либо рвёт генерацию, либо не ловит обрыв.
- Wi-Fi-специфика: streaming-ответ может оборваться в середине. Клиент обязан отдавать
  частичный ответ вверх, а не терять ход целиком.
- `config/system_prompt.txt` должен быть **побайтово тем же**, что шлёт `llm_client` —
  иначе прогрев префикса бесполезен. Отметить в README как точку рассинхрона;
  в идеале позже сделать симлинк/генерацию из одного источника.

## 7. Выбор модели

Профили в `config/models/`. Дефолт для ноута — 7–8B Q4_K_M; `Qwen2.5-3B` / `Phi-3-mini`
остаются профилями для Jetson, на дискретной GPU ноута они избыточно слабы.

Для русскоязычного музейного сценария стоит сравнить с русско-тюненными вариантами на
базе Qwen2.5 (линейки T-lite / Vikhr). **Проверить наличие GGUF-сборок и лицензию** —
не полагаться на это утверждение без проверки. Сравнивать по: качеству русского,
стабильности следования GBNF-грамматике, TTFT из `bench_ttft.py`.

## 8. Порядок реализации

1. `.env.example` + `docker-compose.yml`, поднять на CPU-теге, `curl /health`.
2. `fetch_model.sh`, скачать одну модель, переключить на `server-cuda`, проверить `nvidia-smi` под нагрузкой.
3. `warmup.sh` + `config/system_prompt.txt`, убедиться что sidecar выходит с 0.
4. `bench_ttft.py`, снять baseline на localhost.
5. `publish_mdns.sh`, `systemd/`, `docs/host_setup.md`.
6. Прогон с Jetson по Wi-Fi, занести цифры в README.

## 9. Критерии приёмки

- `docker compose up -d` с нуля на чистой машине → `/health` = 200 без ручных шагов, кроме `.env` и `fetch_model.sh`.
- `llm-warmup` завершается с кодом 0; TTFT второго запроса заметно ниже первого (подтверждает работу префикс-кэша).
- Смена `MODEL_FILE` в `.env` + `docker compose up -d` меняет модель без пересборки.
- Порт недоступен с произвольного хоста в сети, доступен с IP Jetson.
- Ребут ноута → сервис поднимается сам, крышка закрыта → сервис жив.
