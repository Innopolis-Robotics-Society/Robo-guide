# guide_robot_api

Приватный ROS-адаптер управления роботом. Публичный API обслуживает независимый
хостовый сервис `scripts/jetson-launcher/guide_launcher`.

- `api_node.py`: ROS-подписки, клиенты, очередь исполнения, публикация снимков и медиа.
- `command_adapter.py`: RunTour/Say operations и ROS services, без собственной mission FSM.
- `lib/http_server.py`: внутренние HTTP/WebSocket на `127.0.0.1:8091`.
- `lib/operations.py`: ограниченный реестр action goals, feedback/results/cancel.
- `config/api.yaml`, `launch/api.launch.py`: параметры и запуск.

Запуск: `ros2 launch guide_robot_api api.launch.py`.
Нужен непустой `bridge_token_file`, совпадающий с секретом хостового сервиса.
Все внутренние маршруты, кроме дешёвого `/healthz`, требуют X-Bridge-Token.
HTTP здесь не предназначен для браузера. Каталог и бизнес-правила остаются в ROS.

Публичный контракт и миграция:
[API v1](../scripts/jetson-launcher/guide_launcher/spec/README.md).
Эталонный UI находится в `reference_ui`, прошивки — в `firmware/operator_rfid`.
Старый пакет `guide_robot_operator_ui` удалён. Пересоберите все потребители
`guide_robot_msgs`: изменены FaceState, MissionState, добавлен OverrideFace.
