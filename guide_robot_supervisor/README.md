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
упадёт с `UnboundLocalError` вместо аккуратного лога. Воспроизводится:
скопировать тело метода, выставить `_stop` до входа — `UnboundLocalError:
cannot access local variable 'bad'`.

---

## Дополнение от 2026-07-31 (второй проход, ничего из этого НЕ исправлено)

Ниже — то, что нашлось при повторном разборе. Пункты 5-15 в коде не трогали;
пакет находится ровно в том состоянии, в котором описан выше.

### Сначала две неточности в этом же README

- **`package.xml:13` — это `nav2_msgs`, а не `lifecycle_msgs`.** Строчки про
  `exec_depend lifecycle_msgs` в манифесте вообще нет. Сам вывод («супервизор
  делегирует переходы менеджерам nav2») верен, ссылка — мусор.
- **Пункт 2 выше описывает механизм неверно.** `_poll_watchdogs(force=True)`
  опрашивает ВСЕ watchdog'и (`supervisor_node.py:264-266` итерируется по
  `self.watchdogs.values()`, `force` лишь обходит `next_check`), а не только
  preconditions текущей группы. Настоящая причина другая и хуже:
  `_apply_policies` вызывается только в состояниях `ACTIVE`/`DEGRADED`
  (`supervisor_node.py:203`), поэтому во время `BRINGUP` политики не
  применяются в принципе; а во время блокирующего `_manage(STARTUP)` (до 45 с)
  не опрашивается вообще ничто. Вывод пункта 2 остаётся в силе, обоснование
  надо заменить.

### 5. Архитектурное: супервизор не управляет `ros2_control`, то есть моторами

Самый важный пункт. Супервизор гейтит ровно три группы
`nav2_lifecycle_manager`: `collision_monitor`, `map_server`+`amcl`, ядро Nav2.
Всё остальное — обычные, не-lifecycle узлы, которые launch поднимает
безусловно и параллельно, ещё до того как супервизор прочитает свой YAML:
`robot_state_publisher`, `ros2_control_node`, **спавнеры
`diff_drive_controller` и `joint_state_broadcaster`**
(`guide_robot_bringup/launch/hardware.launch.py:127-155`), лидары, мерджер,
`sonar_node`, `foxglove_bridge`, `rviz2`.

Причина зафиксирована прямо в конфиге —
`guide_robot_description/config/controllers.yaml.in:10-16`:

```yaml
# TEMPORARY WORKAROUND / ВРЕМЕННОЕ РЕШЕНИЕ:
# Изначально компонент планировался в 'unconfigured', чтобы supervisor управлял
# активацией hardware. До завершения интеграции supervisor с ros2_control
# держим 'active', чтобы спавнеры и контроллеры поднимались автоматически.
hardware_components_initial_state:
  active:
    - GuideRobotSystem
```

То есть привод активируется при старте launch, мимо супервизора. Весь словарь
супервизора — `nav2_msgs/ManageLifecycleNodes`; обращений к
`controller_manager` (`SetHardwareComponentState`, `SwitchController`) в
пакете нет ни одного. Практический эффект: при запуске без сонаров/моторов
`safety`/`localization`/`navigation` действительно не активируются (это
работает), но моторный стек и все драйверы поднимаются как ни в чём не
бывало, а останавливает моторы при отсутствующем железе не супервизор, а
отказ `on_configure` в самом плагине `guide_robot_hardware`.

Направление решения: обобщить `Group` с «менеджера nav2» до «бэкенда» и
добавить тип группы, работающий через `controller_manager_msgs`, после чего
убрать `hardware_components_initial_state: active`. Сигнатуры сервисов надо
сверять по **Humble**, а не по хостовому Jazzy — между релизами менялись.

### 6. `estop` — холостая операция, и топик не latched

`_do_action` для `estop` (`supervisor_node.py:314-316`) публикует
`Bool(true)` и **сразу возвращается**: никаких lifecycle-команд. При этом на
`/supervisor/estop` во всём репозитории не подписан никто (проверено грепом по
`.py/.yaml/.in/.cpp/.xml`). Единственная проба с этой политикой — `tf_odom`
(`config/supervisor.yaml:65-68`), то есть потеря `odom → base_link`, самый
опасный отказ для едущего робота. Реакция на него сегодня: одно сообщение в
топик, который никто не читает.

Плюс паблишер обычный (`supervisor_node.py:126-128`, depth 10, VOLATILE) и
публикуется только по фронту — подписчик, поднявшийся позже, не узнает о
срабатывании никогда. Нужен `TRANSIENT_LOCAL` depth 1 и стартовая публикация
`false`.

Если чинить: `estop` должен ещё и деактивировать группы, способные командовать
движением, но **не** `safety` — усыпить `collision_monitor` значит сделать
прямо противоположное аварийной остановке.

### 7. `_bringup()` реентерабелен, а `self._lock` не защищает ничего

RLock создаётся на `supervisor_node.py:104` и используется ровно один раз —
прочитать `self._state` на `:197-198`. `_srv_bringup` (`:350`) выполняется в
потоке `MultiThreadedExecutor` и вызывает `_bringup()` напрямую, параллельно с
рабочим потоком, который при `autostart=true` и состоянии `INIT` делает то же
самое. Две одновременные последовательности `STARTUP` по одним и тем же
менеджерам. `_srv_reset` (`:360`) аналогично переставляет состояние в `INIT`
посреди чужого bring-up.

### 8. Один мёртвый сонар блокирует весь стек

`sonar_rate` требует **всех семи** топиков (`topic_rate.py:100`: `dead` — любой
топик ниже `min_rate`), это precondition группы `safety`, `safety` не
`optional`, а `localization`/`navigation` висят на ней через `requires`. Итог:
один отвалившийся датчик → 60 с ожидания → `FAULT` → не поднялось ничего,
выход только ручным `~/reset`. Механизма «N из 7» в `TopicRateWatchdog` нет.

Это не обязательно баг — мёртвый сонар это слепой сектор у 62-килограммовой
базы среди людей. Но решение должно быть явным (параметр `min_alive`), а не
следствием того, что альтернативы не предусмотрено.

### 9. Непроверенный watchdog удовлетворяет precondition

`WatchdogSlot.status` по умолчанию `Status(Level.OK, "not run yet")`
(`supervisor_node.py:79`), а `_wait_preconditions` проверяет только
`status.level != Level.OK` (`:216`). Сейчас это замаскировано тем, что
`_poll_watchdogs(force=True)` вызывается до проверки, но инвариант держится
случайно: любая перестановка кода даст гейт, который пропускает всё. Нужен
отдельный флаг «проверялся хотя бы раз».

### 10. Неизвестное имя в `preconditions` молча снимает гейт

`supervisor_node.py:216`: `if n in self.watchdogs and ...`. Опечатка в имени
watchdog'а не ошибка, а тихое исчезновение условия — группа стартует без
проверки вообще. Валидации конфига на старте нет никакой.

### 11. Опечатка в YAML роняет узел без диагностики

`Group(**g)` (`:110`) и `Policy(**spec.get("policy", {}))` (`:150`) — лишний
или переименованный ключ даёт `TypeError` из `Supervisor.__init__`, наружу
уходит голый traceback без имени группы/watchdog'а. Не валидируются также:
неизвестная группа в `requires`/`target`, неизвестный `on_error`, дубли имён.

### 12. `_undo_action` для `reset` поднимает никогда не поднятые группы

`supervisor_node.py:345-347` делает `STARTUP` любой группе в состоянии
`INACTIVE`, а `_targets` при `target: None` возвращает вообще все. Плюс
`_do_action` (`:322-330`) выставляет `grp.state` независимо от успеха
`_manage`, и если два watchdog'а усыпили одну группу, первый же
восстановившийся её разбудит. Сейчас недостижимо — ни одна политика в обоих
конфигах не использует `reset`.

### 13. `destroy_node` не джойнит рабочий поток

`supervisor_node.py:402-406`: `self._stop.set()`, затем сразу
`super().destroy_node()`, пока `_tick` может быть в середине вызова сервиса.
Поток `daemon`, процесс выйдет, но на shutdown возможен мусорный traceback.
`call_sync` и `_run` при этом на `_stop` не смотрят.

### 14. `nav_stack.launch.py` по умолчанию нарушает контракт `autostart: false`

Докстринг супервизора требует, чтобы все lifecycle-менеджеры запускались с
`autostart: false`. `hardware.launch.py:76` и `simulation.launch.py` передают
`false` явно, а `nav_stack.launch.py:69` объявляет `default_value="true"` —
прямой запуск этого файла с дефолтами даёт nav2, поднимающийся сам,
параллельно с супервизором. Проверки этого условия в супервизоре нет; дёшево
добавить пробу `<manager>/is_active` перед `STARTUP` и предупреждать.

### 15. Мелочи

- `TopicRateWatchdog.check()` вызывает `_rate(t)` трижды на топик
  (`topic_rate.py:99-101`), каждый раз заново читая часы; классификация одного
  и того же топика внутри одной проверки может разъехаться.
- `LifecycleManagerActiveWatchdog` не имеет ни `grace`, ни `reset()`, в отличие
  от трёх остальных проб.
- `maintainer email` расходится: `package.xml:7` — `mook@example.com`,
  `setup.py` — `mook@innopolis.university`.
- Окно `grace` во всех пробах привязано к `setup()`; отдельного «армирования» в
  момент, когда группа реально начинает зависеть от пробы, нет (это пункт 3
  выше, здесь — как напоминание, что чинится он в `WatchdogBase`).
