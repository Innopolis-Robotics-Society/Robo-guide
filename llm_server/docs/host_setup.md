# Настройка хоста

Ручные шаги для хоста, на котором крутится `iros-llm` (ноут-разработчика или
любой другой сервер с NVIDIA GPU). Это модификация системного конфига —
намеренно не автоматизировано скриптом.

## 1. nvidia-container-toolkit

Проверка, что Docker видит GPU:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

Если команда падает — установить/переустановить
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
и перезапустить Docker (`sudo systemctl restart docker`).

## 2. Файрвол

Порт `LLM_PORT` должен быть доступен **только** с IP Jetson, не всей сети:

```bash
sudo ufw allow from <jetson_ip> to any port <LLM_PORT> proto tcp
```

Не открывать порт в `0.0.0.0/0`. Если IP Jetson не статический — см. DHCP-резервацию
ниже, иначе правило придётся переписывать при каждой смене адреса.

## 3. Запрет сна ноутбука

Демо не переживает закрытие крышки или уход в suspend. Два шага:

```bash
# /etc/systemd/logind.conf
# HandleLidSwitch=ignore
sudo sed -i 's/^#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
sudo systemctl restart systemd-logind

sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## 4. AP isolation

На музейном/гостевом Wi-Fi клиенты часто изолированы на уровне L2 — формально
одна сеть, но Jetson не видит ноут напрямую. Проверять до деплоя:

```bash
# с Jetson
ping <ip_ноута>
```

Если не пингуется — AP isolation включена на точке доступа. Fallback: свой
роутер (взять с собой) или прямой Ethernet-линк Jetson↔ноут.

## 5. Адресация: DHCP-резервация, а не mDNS

Основной путь — **DHCP-резервация по MAC на роутере** + явный IP в
ROS-параметре `llm.base_url`. `scripts/publish_mdns.sh` — опциональное
удобство при локальной разработке (`iros-llm.local` не зависящий от
hostname), в проде не полагаться: mDNS-резолвинг из Docker-контейнера на
Jetson требует `libnss-mdns` и проброса host-DNS, работает не везде и молча
деградирует, ищите проблему по факту "почему LLM недоступен" вместо явной
ошибки.

## 6. Автозапуск через systemd

```bash
sudo mkdir -p /opt/iros-llm-server
sudo rsync -a --exclude models ./ /opt/iros-llm-server/   # или git clone прямо туда
sudo cp systemd/iros-llm.service /etc/systemd/system/
# поправить WorkingDirectory/-f в /etc/systemd/system/iros-llm.service,
# если путь деплоя отличается от /opt/iros-llm-server
sudo systemctl daemon-reload
sudo systemctl enable --now iros-llm.service
```

Ребут ноутбука → сервис поднимается сам (`docker compose up -d`, контейнеры
`restart: unless-stopped` переживут рестарт демона Docker).

## 7. Проверка готовности

```bash
curl -s localhost:<port>/health
curl -s localhost:<port>/props    # отдаёт загруженную модель и ctx_size — сверить с .env
```

`/health` = `{"status":"ok"}` и `/props.model_path` соответствует `MODEL_FILE` из `.env`
означает, что стек поднят корректно.
