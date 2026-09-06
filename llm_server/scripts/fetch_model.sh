#!/usr/bin/env bash
# Скачивает GGUF-модель с HuggingFace (resolve/main) в MODELS_DIR.
# Без huggingface-cli — только curl, чтобы не тащить лишнюю зависимость на хост.
set -euo pipefail

usage() {
  echo "Usage: $0 <repo_id> <filename> [dest_dir]" >&2
  echo "  repo_id   HF repo, напр. bartowski/Qwen2.5-7B-Instruct-GGUF" >&2
  echo "  filename  имя файла в репозитории, напр. Qwen2.5-7B-Instruct-Q4_K_M.gguf" >&2
  echo "  dest_dir  по умолчанию ./models" >&2
  exit 1
}

[ "$#" -ge 2 ] || usage

repo_id="$1"
filename="$2"
dest_dir="${3:-./models}"
url="https://huggingface.co/${repo_id}/resolve/main/${filename}"
dest="${dest_dir}/${filename}"

auth_header=()
if [ -n "${HF_TOKEN:-}" ]; then
  auth_header=(-H "Authorization: Bearer ${HF_TOKEN}")
fi

mkdir -p "$dest_dir"

echo "==> HEAD ${url}" >&2
# -L: HF отдаёт редирект на CDN, реальный Content-Length только в финальном ответе.
# tail -1: берём последний заголовок content-length по цепочке редиректов.
remote_size="$(curl -sIL --fail "${auth_header[@]}" "$url" \
  | tr -d '\r' | grep -i '^content-length:' | tail -1 | awk '{print $2}')"

if [ -z "${remote_size:-}" ]; then
  echo "error: не удалось определить размер файла (HEAD не удался или нет content-length)" >&2
  exit 1
fi

if [ -f "$dest" ]; then
  local_size="$(stat -c%s "$dest")"
  if [ "$local_size" = "$remote_size" ]; then
    echo "==> ${dest} уже скачан (${local_size} байт), пропускаю" >&2
    exit 0
  fi
  echo "==> ${dest} существует, но размер не совпадает (локально=${local_size}, на сервере=${remote_size}), докачиваю" >&2
fi

avail_kb="$(df --output=avail "$dest_dir" | tail -1)"
avail_bytes=$((avail_kb * 1024))
if [ "$avail_bytes" -lt "$remote_size" ]; then
  echo "error: недостаточно места в ${dest_dir} (нужно ${remote_size} байт, доступно ${avail_bytes})" >&2
  exit 1
fi

echo "==> скачиваю ${remote_size} байт -> ${dest}" >&2
curl -L --fail --continue-at - "${auth_header[@]}" -o "$dest" "$url"

echo "==> готово: ${dest}" >&2
