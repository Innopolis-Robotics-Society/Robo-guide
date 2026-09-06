#!/bin/sh
# One-shot warmup для llama-server: ждёт готовности, затем прогревает
# KV-кэш статического префикса (system prompt) одним запросом с max_tokens=1.
# POSIX sh: образ curlimages/curl -- slim alpine, только curl+busybox, без bash/python/jq.
set -eu

LLM_URL="${LLM_URL:-http://llm:8080}"
WARMUP_TIMEOUT_S="${WARMUP_TIMEOUT_S:-600}"
SYSTEM_PROMPT_FILE="${SYSTEM_PROMPT_FILE:-/system_prompt.txt}"
INTERVAL_S=2

do_curl() {
  if [ -n "${LLM_API_KEY:-}" ]; then
    curl -H "Authorization: Bearer ${LLM_API_KEY}" "$@"
  else
    curl "$@"
  fi
}

# Экранирует содержимое файла в валидную JSON-строку (без jq: \, ", реальные
# переводы строк -> \n). AWK/SED тут -- busybox-варианты из образа curl.
json_escape_file() {
  sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' "$1" | awk 'BEGIN{ORS="\\n"}{print}' | sed 's/\\n$//'
}

echo "warmup: waiting for ${LLM_URL}/health (timeout ${WARMUP_TIMEOUT_S}s)" >&2
elapsed=0
ready=0
while [ "$elapsed" -lt "$WARMUP_TIMEOUT_S" ]; do
  code="$(do_curl -s -o /dev/null -w '%{http_code}' "${LLM_URL}/health" || echo 000)"
  if [ "$code" = "200" ]; then
    ready=1
    break
  fi
  sleep "$INTERVAL_S"
  elapsed=$((elapsed + INTERVAL_S))
done

if [ "$ready" -ne 1 ]; then
  echo "warmup: timeout -- ${LLM_URL}/health not ready after ${WARMUP_TIMEOUT_S}s" >&2
  exit 1
fi
echo "warmup: server ready after ~${elapsed}s, priming prefix cache" >&2

sys_content="$(json_escape_file "$SYSTEM_PROMPT_FILE")"
body=$(printf '{"messages":[{"role":"system","content":"%s"},{"role":"user","content":"%s"}],"max_tokens":1,"stream":false}' \
  "$sys_content" "Здравствуй.")

resp_file="$(mktemp)"
http_code="$(do_curl -s -o "$resp_file" -w '%{http_code}' -X POST "${LLM_URL}/v1/chat/completions" \
  -H 'Content-Type: application/json' -d "$body")"

if [ "$http_code" != "200" ]; then
  echo "warmup: POST /v1/chat/completions failed, HTTP ${http_code}" >&2
  cat "$resp_file" >&2
  rm -f "$resp_file"
  exit 1
fi

rm -f "$resp_file"
echo "warmup: ok, prefix primed" >&2
exit 0
