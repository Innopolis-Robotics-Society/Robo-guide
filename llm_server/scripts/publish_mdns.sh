#!/usr/bin/env bash
# Публикует стабильное mDNS-имя для этого хоста, не зависящее от системного
# hostname. Опциональное удобство для разработки -- см. README.md/host_setup.md:
# основной путь адресации Jetson -> ноут -- DHCP-резервация по MAC + явный IP
# в ROS-параметре, а не mDNS (в контейнере на Jetson резолвинг .local не
# гарантирован без libnss-mdns и проброса host-DNS).
set -euo pipefail

name="${1:-iros-llm}"
name="${name%.local}"

if ! command -v avahi-publish >/dev/null 2>&1; then
  echo "error: avahi-publish не найден. Установить: sudo apt install avahi-utils" >&2
  exit 1
fi

ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')"
if [ -z "${ip:-}" ]; then
  echo "error: не удалось определить IP интерфейса дефолтного маршрута" >&2
  exit 1
fi

echo "==> публикую ${name}.local -> ${ip} (Ctrl+C для остановки)" >&2
exec avahi-publish -a -R "${name}.local" "$ip"
