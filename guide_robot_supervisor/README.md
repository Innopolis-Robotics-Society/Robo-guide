# guide_robot_supervisor

Пакет `ament_python`, реализующий верхнеуровневый супервизор жизненного цикла
робота Guide-Robot: упорядоченный bring-up групп `nav2_lifecycle_manager` и
набор подключаемых watchdog'ов, которые после bring-up непрерывно следят за
здоровьем стека и применяют политики (`warn` / `pause` / `reset` / `shutdown`
/ `estop`).

## Обзор

Точка входа — `guide_robot_supervisor/supervisor_node.py:85` (класс
`Supervisor(Node)`). Узел:

1. Читает `config_file` (обязательный параметр, путь к YAML, по умолчанию
   `config/supervisor.yaml`) и строит из него:
   - упорядоченный список групп (`Group`, `supervisor_node.py:52-62`) —
     каждая группа оборачивает один `ManageLifecycleNodes`-сервис
     (`<manager>/manage_nodes`) нав2-подобного lifecycle-менеджера;
   - набор watchdog'ов (`WatchdogSlot`, `supervisor_node.py:74-82`),
     динамически импортируемых по dotted-path из `class:` в YAML
     (`_add_watchdog`, `supervisor_node.py:144-151`).
2. Публикует состояние в `~/state` (`std_msgs/String`) и агрегированную
   диагностику в `~/diagnostics` (`diagnostic_msgs/DiagnosticArray`,
   `_publish_diagnostics`, `supervisor_node.py:370-400`).
3. Предоставляет сервисы `~/bringup`, `~/shutdown`, `~/reset`
   (`std_srvs/Trigger`) для ручного управления.
4. Публикует общий `estop`-топик (`/supervisor/estop` по умолчанию,
   `Bool`) — используется как сигнал аварийной остановки для остального
   стека.

FSM всего пять «активных» состояний плюс `INIT`/`SHUTDOWN`
(`SupervisorState`, `supervisor_node.py:42-49`): `INIT → BRINGUP → ACTIVE ⇄
DEGRADED`, а также терминальные `FAULT` (требует ручного `~/reset`) и
`RECOVERING` (переходное, во время `_undo_action`). Рабочий цикл крутится в
отдельном `threading.Thread` (`self._worker`, `supervisor_node.py:137-141`,
метод `_run`/`_tick`, `supervisor_node.py:187-205`) с периодом
`loop_period` (по умолчанию 0.2 с), а ROS-колбэки (сервисы, подписки
watchdog'ов, service-клиенты) крутятся в `MultiThreadedExecutor` с 4 потоками
(`main()`, `supervisor_node.py:409-421`). Такое разделение — оправданное
решение: блокирующие `call_sync` (см. ниже) не блокируют executor.

## Управление жизненным циклом узлов

Оркестрация построена вокруг `nav2_msgs/srv/ManageLifecycleNodes`
(`STARTUP` / `SHUTDOWN` / `RESET` / `PAUSE` / `RESUME`), т.е. супервизор
управляет не отдельными `LifecycleNode`, а группами, уже собранными под
`nav2_lifecycle_manager` (`/lifecycle_manager_safety`,
`/lifecycle_manager_localization`, `/lifecycle_manager_navigation` —
`config/supervisor.yaml:6-22`). Прямых вызовов `lifecycle_msgs`-сервисов
(`change_state`/`get_state`) для конкретных `LifecycleNode` в пакете нет,
несмотря на exec_depend `lifecycle_msgs` в `package.xml:13` — supervisor
делегирует переходы состояний менеджерам nav2.

Порядок bring-up = порядок групп в `config/supervisor.yaml` (`safety →
localization → navigation`), shutdown — в обратном порядке
(`_shutdown_all`, `supervisor_node.py:255-261`). Перед `STARTUP` каждой
группы:
- проверяются `requires` (зависимости должны быть `ACTIVE`,
  `_bringup`, `supervisor_node.py:224-234`);
- ожидаются `preconditions` — список имён watchdog'ов, которые должны
  быть `Level.OK` (`_wait_preconditions`, `supervisor_node.py:208-222`), с
  собственным `precondition_timeout` на группу.

Блокирующие вызовы сервисов реализованы в `call_sync`
(`supervisor_node.py:154-168`): ждут готовности сервиса, затем poll'ят
`future.done()` в цикле `time.sleep(0.02)` до `timeout`, при истечении —
`future.cancel()` и `None`. Таймаут **есть** на каждом вызове (`_manage`,
`supervisor_node.py:176-184`, вызывается с `grp.startup_timeout` /
`10.0` / `15.0` в зависимости от команды) — в этом плане пакет
защищён от зависшего lifecycle-сервиса лучше, чем можно было ожидать.
Слабое место: `future.cancel()` не отменяет запрос на стороне сервера —
это лишь локальная пометка, ответ, пришедший позже, останется висеть.

## Watchdog'и

Общий контракт — `watchdogs/base.py`: `WatchdogBase.check() -> Status`,
`Status.level` из `Level(IntEnum)` (`OK=0, WARN=1, ERROR=2, STALE=3`),
`Status.bad` истинен **только** для `ERROR`/`STALE` (`base.py:41-42`) —
`WARN` никогда не считается «плохим» для целей debounce/policy.

Реализованные watchdog'и (`guide_robot_supervisor/watchdogs/`):

- **`TopicRateWatchdog`** (`topic_rate.py`) — частота публикации набора
  топиков через скользящее окно меток времени; учитывает деградацию
  (`_rate`, строки 75-86: если последнее сообщение «протухло» дольше
  `2/min_rate`, отдаёт 0 Гц, а не историческую частоту). Используется для
  сонаров и лидаров (`config/supervisor.yaml:28-63`).
- **`TFWatchdog`** (`tf_available.py`) — наличие и «свежесть» трансформа
  `parent → child` через общий `tf2_ros.Buffer`, один на узел
  (`hasattr(self.node, "_tf_buffer")`, строки 27-30) — разумно экономит
  подписки на `/tf`.
- **`NodeAliveWatchdog`** (`node_alive.py:12-37`) — присутствие имени узла
  в графе ROS (`get_node_names_and_namespaces`). Это проверка **регистрации
  в графе**, а не livenes/responsiveness — «зомби»-узел, всё ещё
  зарегистрированный, но не обрабатывающий колбэки, не будет обнаружен.
- **`LifecycleManagerActiveWatchdog`** (`node_alive.py:39-66`) — дергает
  `<manager>/is_active` (`std_srvs/Trigger`).

Общая политика: `check()` дергается из `_poll_watchdogs`
(`supervisor_node.py:264-277`) с периодом `policy.period`; при `bad` —
`strikes += 1`, иначе сброс в 0. В `_apply_policies`
(`supervisor_node.py:279-303`) `strikes >= policy.debounce` защёлкивает
(`latched`) — выполняется `_do_action` (пауза/сброс/шатдаун/эстоп группы
или всех), обратный переход (`recover: true` и статус снова `OK`) выполняет
`_undo_action`.

**Найденный дефект в `LifecycleManagerActiveWatchdog.check()`
(`node_alive.py:55-56`):** если сервис `<manager>/is_active` вообще
недоступен (`not self._cli.service_is_ready()`), метод **всегда**
возвращает `Level.WARN`, без grace-эскалации в `ERROR`, в отличие от
`NodeAliveWatchdog`/`TFWatchdog`/`TopicRateWatchdog`, у которых есть
параметр `grace` и переход WARN→ERROR/STALE по таймауту. Поскольку `WARN`
не входит в `Status.bad`, `strikes` никогда не растёт — то есть **полный
краш процесса** `lifecycle_manager_safety` (когда сервис пропадает
насовсем, а не просто отвечает `success=false`) watchdog `safety_alive`
(`config/supervisor.yaml:82-85`, `policy: on_error: pause`) никогда не
обнаружит. Это тихий false negative именно для самого тяжёлого случая
отказа — падения менеджера целиком.

## Известные проблемы и замечания


### 1. Отсутствие защиты от «дребезга» (restart-loop protection)

В `_apply_policies`/`_do_action`/`_undo_action`
(`supervisor_node.py:279-347`) нет ни cooldown, ни счётчика попыток между
срабатываниями. Если watchdog колеблется около границы `debounce` (типичная
ситуация для `sonar_rate`/`scan_rate` при нестабильном UART), группа будет
уходить в `pause`/`reset` и возвращаться обратно без ограничений по частоте
— в отличие, например, от `replan_cooldown_sec` в MAPF-планере этого же
репозитория. Стоит добавить минимальный интервал между действиями и/или
счётчик «сдался после N попыток → эскалация».

### 2. Watchdog'и блокируются во время `_bringup()`

`_wait_preconditions` форсит проверку (`_poll_watchdogs(force=True)`)
только для watchdog'ов из `preconditions` текущей группы
(`supervisor_node.py:213-217`). Остальные (например `control_stack`,
`safety_alive`) не проверяются, пока `_bringup()` выполняется внутри
`_tick()` синхронно — рабочий поток занят до 30-45 с на группу (плюс до
60 с ожидания preconditions). Отказ `controller_manager` в этот момент
будет замечен только после завершения всего bring-up.

### 3. Окно `grace` привязано к моменту конструирования, а не к моменту актуальности

`grace`-таймер watchdog'ов (`NodeAliveWatchdog._t0`, `TFWatchdog._t0`,
`TopicRateWatchdog._t0`) стартует один раз в `setup()` — в момент создания
супервизора, а не когда группа реально начинает от него зависеть. При
медленном bring-up (safety выбирает почти весь `precondition_timeout`
60 с + `startup_timeout` до 30 с) watchdog'и `localization`/`navigation`
(`tf_odom` grace 20 с, `tf_map` grace 30 с) к моменту фактической проверки
могут уже перейти из WARN в ERROR/STALE, хотя реального шанса стать `OK`
у них ещё не было. `_wait_preconditions` всё равно ждёт `Level.OK`
независимо от уровня, поэтому bring-up не ломается — но диагностика в этот
момент вводит в заблуждение.

### 4. Незакрытый edge-case в `_wait_preconditions`

`supervisor_node.py:221` использует переменную `bad`, объявленную только
внутри тела `while` (строка 214). Если условие цикла ложно уже на первой
проверке (например, `self._stop.is_set()` стал `True` ровно в момент
входа — гонка при shutdown во время bring-up), `bad` не определена и метод
упадёт с `UnboundLocalError` вместо аккуратного лога.
