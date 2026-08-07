# iros_llm_server

Контейнеризованный inference-бэкенд для `guide_robot_llm`: Docker Compose поверх
upstream-образа `llama.cpp`, без пересборки при смене модели/параметров. Живёт вне
ROS-монорепо намеренно — не ROS-пакет, не собирается через `colcon`. Что входит и что
осознанно не входит (failover, GBNF, chat-темплейты) — см. `iros_llm_server_SPEC.md` §0.

Контракт наружу: `http://<host>:<port>/v1` (OpenAI-compatible) + `GET /health` + `GET /metrics`.

## Быстрый старт

```bash
cp .env.example .env
# при необходимости поправить LLAMA_TAG/LLM_BIND/модель — см. комментарии в .env

./scripts/fetch_model.sh bartowski/Qwen2.5-7B-Instruct-GGUF Qwen2.5-7B-Instruct-Q4_K_M.gguf

docker compose up -d
curl -s localhost:8080/health   # {"status":"ok"}
```

`docker compose up -d` поднимает оба сервиса: `llm` (сам сервер) и `llm-warmup`
(one-shot сайдкар, прогревает KV-кэш системного промпта и завершается кодом 0).
Проверить: `docker compose ps` — `llm-warmup` в `Exited (0)`.

Смена модели: поменять `MODEL_FILE`/`MODEL_ALIAS` в `.env` (см. готовые профили в
`config/models/`), `./scripts/fetch_model.sh <repo> <file>`, `docker compose up -d`.
Пересборка не нужна.

## Профили моделей

| Профиль | Модель | Когда | Файл |
|---|---|---|---|
| `qwen7b-q4` (дефолт) | Qwen2.5-7B-Instruct Q4_K_M | ноут, дискретная GPU ≥8GB VRAM | `config/models/qwen7b-q4.env` |
| `cpu-fallback` | Qwen2.5-3B-Instruct Q4_K_M | CPU-смоук, слабое железо, `LLAMA_TAG=server` | `config/models/cpu-fallback.env` |

Применить профиль: скопировать нужные строки из `config/models/<profile>.env` поверх
одноимённых в `.env`.

Для русскоязычного музейного сценария стоит дополнительно сравнить с русско-тюненными
моделями на базе Qwen2.5 (линейки T-lite/Vikhr) — по качеству русского, стабильности
следования GBNF-грамматике (грамматика приходит per-request от `guide_robot_llm`,
сервер про неё не знает) и TTFT. Наличие готовых GGUF-сборок и лицензию не проверяли —
см. SPEC §7.

## Известные точки дрейфа/рассинхрона

- **`LLAMA_ARG_NO_CONTEXT_SHIFT` не существует в текущем upstream-теге.** SPEC §2
  называет эту переменную, но на `ghcr.io/ggml-org/llama.cpp:server`
  (`llama-server --help`, проверено при реализации) актуальное имя —
  **`LLAMA_ARG_CONTEXT_SHIFT`** (булево, `0` = запрет сдвига контекста). `docker-compose.yml`
  использует актуальное имя. Имена `LLAMA_ARG_*` дрейфуют между релизами upstream-образа —
  при обновлении `LLAMA_TAG` перепроверять через
  `docker run --rm --entrypoint /app/llama-server ghcr.io/ggml-org/llama.cpp:<tag> --help`.
- **`config/system_prompt.txt` синхронизирован с преамбулом `guide_robot_llm`,
  но не с полным системным промптом.** `dialog_agent` (шаг 5 `llm_plam.md`)
  теперь реализован и реально шлёт системный промпт: канонический текст —
  `guide_robot_llm/config/system_prompt.txt`, этот файл — его точная копия.
  Но реальное сообщение, которое `dialog_agent` кладёт в `messages[0]`, —
  этот преамбул + каталог инструментов, сгенерированный из
  `tools/schema.py` (`dialog/prompt.py:build_system_prompt`), а не голый
  файл: каталог зависит от кода, не от статичного текста, синхронизировать
  его сюда копипастой означало бы дублировать источник истины. Поэтому
  `llm-warmup` греет ТОЛЬКО префикс до каталога включительно — частичный,
  не полный прогрев `CACHE_REUSE` (полезно, но TTFT второго реального хода
  всё ещё включает префилл каталога инструментов). Полный фикс — как и
  раньше, генерация `config/system_prompt.txt` из `guide_robot_llm` при
  сборке/деплое вместо ручной копии; не сделано в этом заходе.

## Обоснования нетривиальных параметров

См. `iros_llm_server_SPEC.md` §2 для полного списка. Коротко:

- `N_PARALLEL=1` — `CTX_SIZE` делится между слотами, ReAct-цикл строго
  последовательный, второй слот только урезал бы контекст.
- `CACHE_REUSE` — префикс (system prompt + описания инструментов) переиспользуется
  между ходами. Требует от `llm_client`: волатильное (`MissionState`, история) —
  строго ПОСЛЕ статики в теле запроса.
- `LLAMA_ARG_CONTEXT_SHIFT=0` — переполнение контекста должно быть видимой ошибкой,
  не молчаливым сдвигом окна (история принадлежит клиенту, barge-in обрезает её сам).

## Бенчмарк (`scripts/bench_ttft.py`)

Только stdlib, ручной разбор SSE. Меряет TTFT (до первого непустого `content` delta),
decode tok/s, total; печатает p50/p95 по `--n` итерациям (первая — холодный кэш,
отбрасывается).

```bash
python3 scripts/bench_ttft.py --url http://localhost:8080/v1/chat/completions --n 11
```

### Результаты

**localhost (baseline)** — профиль `qwen7b-q4`, RTX 4070 Laptop (8GB VRAM),
`LLAMA_TAG=server-cuda`, `CTX_SIZE=16384`, `N_GPU_LAYERS=999`, промпт — системный
промпт + короткий вопрос (~90 слов), `--n 11 --max-tokens 100`:

```
[1/11] прогрев (отброшен): TTFT=46.5ms total=1723.9ms tokens=80 decode=47.7tok/s
[2/11] измерение: TTFT=26.0ms total=1494.0ms tokens=72 decode=49.0tok/s
...
[11/11] измерение: TTFT=29.2ms total=1812.9ms tokens=85 decode=47.7tok/s

N измерений (без прогрева): 10
TTFT   p50=29.1ms   p95=32.8ms
decode p50=48.0tok/s   p95=49.4tok/s
total  p50=1692.8ms   p95=2074.6ms
```

TTFT первой (холодной) итерации — 46.5ms против p50=29.1ms на прогретых — эффект
префикс-кэша заметен даже в рамках одного запуска `bench_ttft.py` (`CACHE_REUSE`
переиспользует совпадающий префикс между последовательными запросами с одинаковым
началом). Отдельно проверено через `llm-warmup`: сайдкар выходит с кодом 0, после
чего первый реальный запрос уже не платит полный prefill статики.

**Jetson по Wi-Fi (L2-бюджет)** — не снято в рамках этой реализации: нет физического
Jetson в среде, где собирался этот модуль. Снять перед деплоем:

```bash
python3 scripts/bench_ttft.py --url http://<ip_ноута>:<port>/v1/chat/completions --n 11
```

с Jetson, при `LLM_BIND=0.0.0.0` на ноуте и файрволом, разрешающим только IP Jetson
(см. `docs/host_setup.md`). Занести результат сюда.

## mDNS (опционально)

`scripts/publish_mdns.sh` публикует стабильное имя `iros-llm.local`, не зависящее от
hostname — удобство для разработки. **Не основной путь адресации**: mDNS-резолвинг из
Docker-контейнера на Jetson требует `libnss-mdns` и проброса host-DNS, работает не
везде и молча деградирует. В проде — DHCP-резервация по MAC + явный IP в ROS-параметре
`llm.base_url`. Подробнее — `docs/host_setup.md`.

## Автозапуск, файрвол, сон ноутбука

См. `docs/host_setup.md` — nvidia-container-toolkit, `ufw`, запрет сна при закрытой
крышке, AP isolation на музейном Wi-Fi, `systemd/iros-llm.service`.

## Интеграция с ROS-стороной

`dialog_agent` (`guide_robot_llm`, реализован — см. `guide_robot_llm/README.md`)
настраивается через `guide_robot_llm/config/llm.yaml`, блок `/**/dialog_agent`:

- `llm.base_urls` — **список** (не одиночный `llm.base_url`), пробуются по
  порядку с retry (`llm_client/ladder.py`) — сейчас один элемент
  (`http://<ip>:<port>/v1`, дефолт `http://127.0.0.1:18080/v1`, см. `.env`
  этого репозитория — отличается от `.env.example`'s `8080`).
- `llm.connect_timeout_s`(2.0)/`llm.read_timeout_s`(30.0) — раздельные, как
  задумано (сеть локальная, генерация идёт секундами).
- `llm.max_attempts_per_backend`(2), `llm.backoff_s`(0.5), `llm.api_key`
  (пусто = без `Authorization`), `llm.max_tokens`(512), `llm.temperature`(0.2).
- Wi-Fi может оборвать streaming-ответ в середине — `llm_client.Backend`
  (`guide_robot_llm/guide_robot_llm/llm_client/backend.py`) читает SSE
  чанками и уже умеет отдавать `BackendTimeout`/`BackendError` вместо
  зависания, но частичный текст при разрыве сейчас не всплывает наверх
  как отдельный случай — оборванный ход просто уходит в `stopped_reason=
  backend_error` целиком (`dialog/loop.py`).

Подробнее про сам план (ReAct-цикл, GBNF, барж-ин) — `iros_llm_server_SPEC.md`
§6 и `guide_robot_llm/llm_plam.md`.

## Критерии приёмки

- [x] `docker compose up -d` с нуля → `/health` = 200 без ручных шагов, кроме `.env` и
      `fetch_model.sh` (проверено на CPU-теге `server`).
- [x] `llm-warmup` завершается с кодом 0.
- [x] TTFT второго запроса заметно ниже первого (подтверждает работу префикс-кэша) —
      46.5ms → p50=29.1ms, см. секцию «Бенчмарк» выше.
- [x] Смена `MODEL_FILE` в `.env` + `docker compose up -d` меняет модель без
      пересборки (структурно; `LLAMA_ARG_MODEL` подставляется из env, `image:` не
      меняется).
- [ ] Порт недоступен с произвольного хоста в сети, доступен с IP Jetson — настраивается
      `ufw` по `docs/host_setup.md`, не проверялось на реальном Jetson.
- [ ] Ребут ноутбука → сервис поднимается сам, крышка закрыта → сервис жив — требует
      `systemd/iros-llm.service` включённым (`systemctl enable`), не проверялось в этой
      среде (нет прав перезагружать хост).
