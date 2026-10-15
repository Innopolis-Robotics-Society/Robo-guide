# guide_robot_supervisor

Супервизор жизненного цикла Guide-Robot (C++17, `ament_cmake`): упорядоченный
bring-up групп `nav2_lifecycle_manager` и watchdog'и, которые после bring-up
следят за стеком и применяют политики `warn` / `pause` / `reset` / `shutdown` /
`estop`.

До 2026-10 пакет был на Python. Внешний контракт перенесён один в один: те же
топики, сервисы, параметры, имена состояний и формат YAML. Список того, что при
переводе исправлено, — ниже.

## Запуск

```bash
ros2 launch guide_robot_supervisor supervisor.launch.py \
  [config_file:=...] [autostart_supervisor:=true] [right_lidar:=true] [use_sim_time:=false]
```

Обычно его включает `guide_robot_bringup/launch/nav_stack.launch.py`
(`supervisor.yaml`, в режиме SLAM — `supervisor_slam.yaml`).

| Параметр узла | По умолчанию | Смысл |
|---|---|---|
| `config_file` | — (обязателен) | YAML с группами и watchdog'ами |
| `autostart` | `true` | `false` — ждать `~/bringup` в `INIT` |
| `loop_period` | `0.2` | период рабочего цикла, с |
| `estop_topic` | `/supervisor/estop` | топик аварийной остановки |
| `drop_topics` | `[]` | убрать топики из всех `TopicRateWatchdog` (launch передаёт `["/scan_right"]` при `right_lidar:=false`) |

## Интерфейсы

| Имя | Тип | Кто читает |
|---|---|---|
| `/supervisor/state` | `std_msgs/String`, каждые `loop_period` и при смене | face aggregator, operator_ui → jetson-launcher |
| `/supervisor/estop` | `std_msgs/Bool`, **reliable + transient_local, depth 1** | `twist_mux` (lock `e_stop`, приоритет 255), operator_ui, mission_fsm |
| `/supervisor/diagnostics` | `diagnostic_msgs/DiagnosticArray`, 1 Гц | люди / rqt |
| `~/bringup`, `~/shutdown`, `~/reset` | `std_srvs/Trigger` | оператор |

Состояния: `INIT → BRINGUP → ACTIVE ⇄ DEGRADED`, переходное `RECOVERING`,
`FAULT` и `SHUTDOWN`. Из `FAULT` сам не выходит: только `~/reset` (погасить всё
и начать заново) или `~/bringup` (дозапустить то, что не поднято).

## Устройство

```
include/guide_robot_supervisor/   src/
  config.hpp     config.cpp        разбор и проверка YAML (yaml-cpp, без ROS)
  status.hpp     status.cpp        Level / Status
  rate_estimator.hpp ...           частота по окну меток (без ROS)
  watchdog.hpp   watchdog.cpp      контракт, контекст, фабрика
  watchdogs.hpp  watchdogs/*.cpp   четыре watchdog'а
  supervisor.hpp supervisor.cpp    узел: FSM, bring-up, политики
                 main.cpp
```

**Потоки.** Всё состояние супервизора (FSM, группы, слоты) меняет только рабочий
поток. Executor (один поток) обслуживает подписки watchdog'ов, ответы
менеджеров и сервисы. Сервисы ничего не делают сами: кладут команду в очередь
и отвечают отложенно (`send_response`), когда рабочий поток её выполнил. Пока
рабочий поток ждёт менеджера или preconditions, он продолжает опрашивать
watchdog'и и публиковать состояние и диагностику (`idle()` → `housekeeping()`).

**Bring-up** (`Supervisor::bringup`) — один проход по группам в порядке YAML.
Группы в `ACTIVE`/`PAUSED` пропускаются, поэтому повторный bring-up ничего не
дёргает зря. Для каждой группы:
1. все `requires` должны быть `ACTIVE`;
2. все `preconditions` должны быть проверены хотя бы раз и стоять в `OK`, ждём
   до `precondition_timeout`;
3. `STARTUP` через `<manager>/manage_nodes`: до `startup_timeout` ждём появления
   сервиса, ещё до `startup_timeout` — ответа.

Отказ не-optional группы — `FAULT`; optional группа пропускается. Если во время
bring-up пришёл `~/shutdown` или `~/reset`, bring-up прерывается между шагами.
При успехе публикуется `estop=false`, состояние — `ACTIVE`.

**Политики** (`applyPolicies`). Плохая проверка (`ERROR`/`STALE`; `WARN`
никогда) — `strikes += 1`, хорошая — сброс. При `strikes >= debounce` слот
защёлкивается и выполняется действие:

| `on_error` | Действие | Отмена при `recover` и статусе `OK` |
|---|---|---|
| `warn` | только лог | — |
| `pause` | `PAUSE` группе `target` (или всем) | `RESUME` |
| `reset` | `RESET` | `STARTUP` |
| `estop` | `estop=true` | `estop=false`, если ничего больше не защёлкнуто |
| `shutdown` | `estop=true`, `SHUTDOWN` всем в обратном порядке, `FAULT` | нет |

### Watchdog'и

`class:` в YAML — имя C++-класса. Старые питоновские dotted-path принимаются
как алиасы.

- **`TopicRateWatchdog`** — `topics`, `msg_type`, `qos` (`sensor`|`default`),
  `min_rate`, `warn_rate` (= 2·`min_rate`), `window` (10), `grace` (10).
  Generic-подписка без десериализации; тип берётся из `msg_type` или из графа,
  подписка создаётся лениво, когда паблишер появится. Все топики должны быть
  живы.
- **`TfEdgeWatchdog`** — `parent`, `child`, `max_age` (2), `grace` (15). Одна общая
  подписка на `/tf` на все рёбра; только прямое ребро на `/tf`, возраст — по
  времени приёма, а не по `header.stamp`.
- **`NodeAliveWatchdog`** — `nodes`, `grace` (15). Узел есть в графе. Это не
  проверка отзывчивости: зависший, но зарегистрированный узел считается живым.
- **`LifecycleManagerActiveWatchdog`** — `manager`, `timeout` (2), `grace` (15).
  `<manager>/is_active`, не блокирует: каждый `check()` забирает прошлый ответ и
  шлёт новый запрос.

`grace` у всех отсчитывается от создания (и от `reset()`): пока окно открыто,
отсутствие данных — `WARN`.

Новый watchdog: класс-наследник `Watchdog` в `watchdogs.hpp`, структура
параметров и разбор в `config.hpp`/`config.cpp`, ветка в `makeWatchdog`.

## Проверка конфига

Конфиг проверяется целиком при старте. Ошибка — фатальная, с файлом, секцией и
строкой, например
`supervisor.yaml: groups[nav] (line 41): unknown key 'startup_timout'`.
Проверяются неизвестные ключи и классы, типы и диапазоны значений, дубли имён,
`requires` на неизвестную или стоящую ниже группу, `preconditions` и `target`
на неизвестные имена.

## Тесты

```bash
colcon build --packages-select guide_robot_supervisor
colcon test --packages-select guide_robot_supervisor && colcon test-result --verbose
```

- `test_config` — оба реальных YAML, значения по умолчанию, ошибки.
- `test_rate_estimator` — расчёт частоты.
- `test_watchdogs` — все watchdog'и на живых паблишерах и сервисах.
- `test_supervisor` — узел целиком с фейковыми lifecycle-менеджерами: порядок
  bring-up, optional/FAULT, preconditions, pause/resume, сервисы, `drop_topics`,
  `shutdown` с latched estop, ошибка конфига.

Тесты создают свои узлы и топики (`/sup_test/...`, кадры `wd_*`), но для полной
изоляции от живого стека их стоит запускать в отдельном домене:
`ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=1`.

## Что исправлено при переводе на C++

Номера — из разбора Python-версии (история файла в git):
- **№4.** `UnboundLocalError` при остановке во время ожидания preconditions.
- **№7.** Параллельные bring-up из сервиса и рабочего потока: теперь очередь
  команд. Вдобавок повторный bring-up не перезапускает уже поднятые группы.
- **№9.** Непроверенный watchdog больше не открывает precondition.
- **№10, №11.** Опечатки и неверные ссылки в YAML больше не снимают проверки
  молча и не роняют узел голым traceback.
- **№13.** Остановка дожидается рабочего потока, watchdog'и уничтожаются после
  executor'а.
- **№6 (частично).** `estop` теперь latched и публикуется `false` при старте.
- **Grace у `LifecycleManagerActiveWatchdog`:** падение менеджера целиком
  (сервис пропал) теперь `ERROR`, а не вечный `WARN`.
- **№2 (частично).** Во время bring-up и ожидания менеджеров watchdog'и
  опрашиваются, диагностика не замирает. Политики по-прежнему применяются
  только в `ACTIVE`/`DEGRADED`.
- **№15.** Частота считается один раз на топик за проверку.
- `shutdown` больше не выводится из `FAULT` действиями других watchdog'ов в том
  же цикле.
- `~/shutdown` гасит и `PAUSED`-группы.
- Опоздавший ответ менеджера удаляется из клиента (`remove_pending_request`).

## Открытые вопросы

Это изменения поведения, требующие решения, поэтому в перевод они не вошли:
- **Моторы вне супервизора.** `ros2_control` поднимается launch'ем сам
  (`hardware_components_initial_state: active` в
  `guide_robot_description/config/controllers.yaml.in`); супервизор управляет
  только группами nav2-менеджеров.
- **`estop` не деактивирует группы**, только публикует флаг. Если добавлять —
  не трогать `safety`: усыпить `collision_monitor` — противоположность аварийной
  остановке. Сейчас политику `estop` не использует ни один watchdog.
- **Один мёртвый сонар роняет весь стек:** `sonar_rate` требует все 7 топиков,
  это precondition не-optional `safety`. Режима «N из M» нет.
- **Нет cooldown** между pause и resume при дребезге датчика.
- **Окно `grace` привязано к старту узла,** а не к моменту, когда группа
  начинает зависеть от проверки.
- **Семантика `reset`:** отмена делает `STARTUP` любой `INACTIVE` целевой
  группе; состояние группы выставляется независимо от успеха команды. Если две
  проверки поставили одну группу на паузу, первая восстановившаяся её снимет.
  Сейчас `reset` в конфигах не используется.
- **`requires` на optional-группу:** если та не поднялась, зависимая
  не-optional группа уходит в `FAULT` (см. комментарий у `navigation` в
  `supervisor.yaml`).
