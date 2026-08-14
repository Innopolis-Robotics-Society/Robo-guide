#!/bin/sh
# Entrypoint-обёртка образа Jetson: поднимает llama-server, ждёт /health,
# прогревает префикс-кэш системного промпта и передаёт сигналы серверу.
#
# На x86-профиле это делает отдельный sidecar-контейнер llm-warmup
# (docker-compose.yml). На Jetson отдельный контейнер не нужен: прогрев --
# часть подъёма единственного процесса, за который отвечает oom_score_adj
# (см. docker-compose.jetson.yml и JETSON_UPDATE.md задача 4). Логика прогрева
# идентична scripts/warmup.sh (та же JSON-эскейпалка, тот же запрос).
#
# POSIX sh: базовый образ l4t-cuda -- Ubuntu, но держим совместимость с dash.
set -eu

SYSTEM_PROMPT_FILE="${SYSTEM_PROMPT_FILE:-/system_prompt.txt}"
WARMUP_TIMEOUT_S="${WARMUP_TIMEOUT_S:-600}"
LLM_URL="http://127.0.0.1:${LLAMA_ARG_PORT:-8080}"

/app/llama-server "$@" &
server_pid=$!

# docker stop шлёт SIGTERM контейнеру (PID 1 = этот скрипт) -- без явной
# переадресации llama-server не получит сигнал и умрёт только по SIGKILL
# после grace period.
forward_term() {
  kill -TERM "$server_pid" 2>/dev/null || true
}
trap forward_term TERM INT

do_curl() {
  if [ -n "${LLAMA_API_KEY:-}" ]; then
    curl -H "Authorization: Bearer ${LLAMA_API_KEY}" "$@"
  else
    curl "$@"
  fi
}

json_escape_file() {
  sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' "$1" | awk 'BEGIN{ORS="\\n"}{print}' | sed 's/\\n$//'
}

echo "entrypoint: waiting for ${LLM_URL}/health (timeout ${WARMUP_TIMEOUT_S}s)" >&2
elapsed=0
ready=0
while [ "$elapsed" -lt "$WARMUP_TIMEOUT_S" ]; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "entrypoint: llama-server процесс завершился во время старта" >&2
    wait "$server_pid"
    exit $?
  fi
  code="$(do_curl -s -o /dev/null -w '%{http_code}' "${LLM_URL}/health" || echo 000)"
  if [ "$code" = "200" ]; then
    ready=1
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [ "$ready" -eq 1 ] && [ -f "$SYSTEM_PROMPT_FILE" ]; then
  echo "entrypoint: server ready after ~${elapsed}s, priming prefix cache" >&2
  sys_content="$(json_escape_file "$SYSTEM_PROMPT_FILE")"
  body=$(printf '{"messages":[{"role":"system","content":"%s"},{"role":"user","content":"%s"}],"max_tokens":1,"stream":false}' \
    "$sys_content" "Здравствуй.")
  if do_curl -s -o /dev/null -X POST "${LLM_URL}/v1/chat/completions" \
      -H 'Content-Type: application/json' -d "$body"; then
    echo "entrypoint: warmup ok, prefix primed" >&2
  else
    echo "entrypoint: warmup POST failed, продолжаем без прогретого кэша" >&2
  fi
elif [ "$ready" -ne 1 ]; then
  echo "entrypoint: warmup timeout -- /health не ответил за ${WARMUP_TIMEOUT_S}s, сервер мог не подняться" >&2
fi

wait "$server_pid"
