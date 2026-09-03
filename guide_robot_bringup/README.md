# guide_robot_bringup

Пакет верхнеуровневой оркестрации запуска робота Guide-Robot (Guide Robot):
собирает воедино `ros2_control` (диффдрайв), два лидара RPLIDAR C1 со
слиянием сканов (`dual_laser_merger`), сонары, Foxglove Bridge,
стек Nav2 (AMCL или SLAM Toolbox), слой экскурсий (`guide_robot_voice` +
`guide_robot_semantic_map` + `guide_robot_mission_control` +
`guide_robot_face`), супервизор
lifecycle-нод (`guide_robot_supervisor`) и RViz. Сам пакет не содержит
"бизнес-логики" робота — только launch-файлы, rviz-конфиги и три
вспомогательные Python-ноды для лидаров.

Тип сборки — `ament_python`.

## Обзор

Слой запуска состоит из семи launch-файлов и трёх консольных нод:

- на реальном роботе точка входа — `hardware.launch.py`;
- в симуляции (Gazebo) — `simulation.launch.py`;
- `nav_stack.launch.py` — safety/localization/navigation + супервизор,
  общий для железа и симуляции;
- `high_level_stack.launch.py` — слой экскурсий (voice + semantic_map +
  mission_control + face), общий для железа и симуляции, подключается ОТДЕЛЬНО
  от `nav_stack.launch.py` (nav-стек обязан подниматься и без него,
  например для чистого картирования);
- `perception.launch.py` / `lidars.launch.py` и `view_robot.launch.py` —
  самостоятельные вспомогательные launch-файлы (первый переиспользуется
  из `hardware.launch.py`, второй — только для просмотра URDF в RViz без
  `ros2_control`).

Управление жизненным циклом Nav2-нод и сервисного слоя (voice/
semantic_map/mission) идёт через `nav2_lifecycle_manager` (по одному
на каждый пакет: `lifecycle_manager_safety/localization/navigation/
voice/semantic_map/mission`) плюс отдельный супервизор
`guide_robot_supervisor`, который поэтапно поднимает группы по
precondition'ам (TF, частота скана/сонаров и т.д.) и зависимостям
(`mission` требует `navigation`+`voice`+`semantic_map`) — см.
`guide_robot_supervisor/config/supervisor.yaml`. Каждый
`lifecycle_manager_*` запускается СВОИМ launch-файлом безусловно (не под
`autostart`-условием) — супервизору нужен живой `~/manage_nodes`-сервис
независимо от того, кто инициирует `STARTUP`; `autostart:=true` на
конкретном launch-файле — это только для самостоятельного подъёма БЕЗ
супервизора (разработка/отладка одного пакета).

## Файлы запуска

### `launch/hardware.launch.py`

Основной launch для реального робота. Поднимает:
`robot_state_publisher` - `ros2_control_node` (`controller_manager`) -
спаунеры `diff_drive_controller` и `joint_state_broadcaster` -
опционально `perception.launch.py`, сонар (`guide_robot_sonar`,
`sonar_node_mult.py`), Foxglove Bridge - `nav_stack.launch.py`
(safety/localization/navigation + супервизор) - `high_level_stack.launch.py`
(voice + semantic_map + mission_control + face) - `llm.launch.py`
(dialog_agent / tool_broker, `autostart:=true`) - опционально RViz2 с
`rviz/hardware.rviz`.

Аргументы: `use_sim_time` (false), `use_mock_hardware` (false),
`launch_sensors` (true), `launch_sonar` (true), `launch_foxglove` (true),
`slam` (false), `slam_params_file`, `map`, `nav` (true), `nav_params_file`,
`launch_rviz` (true), `autostart_supervisor` (true), `autostart_nav` (false),
`launch_high_level` (true), `launch_face` (true), `launch_llm` (true).

`autostart_supervisor:=false` оставляет супервизор в `INIT` — стек
поднимается только по вызову сервиса `/supervisor/bringup`; политики
watchdog'ов до этого не действуют. `autostart_nav` уходит в
`nav2_lifecycle_manager` и должен оставаться `false`, пока группами
управляет супервизор (см. `guide_robot_supervisor/config/supervisor.yaml`).
`launch_high_level:=false` поднимает только nav-стек, без слоя экскурсий
(например, для чистого картирования/локализации). `launch_face:=false`
оставляет голос/карту/миссию, но не поднимает HTTP-лицо.
`launch_llm:=false` — без dialog_agent; по умолчанию LLM в hardware с
`autostart:=false`, activate делает supervisor (группа `llm` после
`semantic_map`), чтобы location_server уже был доступен.

### `launch/lidars.launch.py`

Два `sllidar_ros2` (`sllidar_left`/`sllidar_right`, RPLIDAR C1,
460800 бод), с правым лидаром, задержанным на 5 с (`lidar_start_delay`)
во избежание просадки питания при одновременном старте. Каждый скан
проходит через `laser_sector_blanker` (свой пакетный exec) — вырезает
угловой сектор, где лидар видит собственное крепление / крепление
второго лидара (жёстко заданные `left/right_blind_sectors_deg`,
откалиброванные вручную через `laser_blind_sector_finder`), затем
`dual_laser_merger` сливает `/scan_left_filtered` +
`/scan_right_filtered` в единый `/scan` в кадре `base_footprint`.
Компенсации движения (deskew) нет — пара сшивается статическим TF,
цена этого меряется `scripts/lidar_lag.py` (см. ниже). Калибровочные
`laser_2_*_offset` получены ICP против общей стены.

Аргументы: `left_port` (`/dev/tty_lidar_left`), `right_port`
(`/dev/tty_lidar_right`), `baudrate`, `use_sim_time`, `merge_frame`,
`lidar_start_delay`.

**Откат deskew 2026-08-11:** на железе снова `dual_laser_merger`.
Питонский `scan_merger` с TF-deskew на Orin под полным стеком ел
~1.4 ядра, а BEST_EFFORT `/scan` в Foxglove/`ros2 topic echo` выглядел
пустым (они по умолчанию RELIABLE). Код ноды оставлен в пакете; на
launch вернётся после порта в C++.

### `launch/perception.launch.py`

Единая точка входа для сенсорики. `real_lidars:=true` (железо) —
`lidars.launch.py` целиком; `real_lidars:=false` (Gazebo) — только
`dual_laser_merger` поверх уже правильных `/scan_left`/`/scan_right`
без калибровки и без deskew'а (сканы в симуляции мгновенные).
`launch_sonar:=true` поднимает `sonar_node_mult.py`.

### `launch/simulation.launch.py`

Точка входа для Gazebo-симуляции: `gazebo.launch.py` из
`guide_robot_simulation`, `dual_laser_merger` через `perception.launch.py`
с `real_lidars:=false` (без калибровочных офсетов — предполагается, что
TF в симуляции точная; deskew там не нужен),
`nav_stack.launch.py` (SLAM Toolbox или AMCL+Nav2 + супервизор,
`autostart_supervisor:=true` — стек сразу сам поднимает ВСЕ группы,
включая `mission`, без ручного `/supervisor/bringup`),
`high_level_stack.launch.py` (voice + semantic_map + mission_control),
RViz с `rviz/sim.rviz`. Аргументы: `slam` (false), `map`, `rviz` (true),
`nav`, `nav_params_file`, `slam_params_file`, `launch_high_level` (true).

### `launch/nav_stack.launch.py`

Общая точка входа для навигации (используется и `hardware.launch.py`,
и `simulation.launch.py`): SLAM Toolbox или AMCL+Nav2
(`guide_robot_navigation`) + `guide_robot_supervisor` (берёт
`supervisor_slam.yaml` при `slam:=true`, иначе `supervisor.yaml`).
Аргументы: `nav` (true), `slam` (false), `map` (`lab_105_full.yaml`), `nav_params_file`,
`slam_params_file`, `autostart_nav` (false), `launch_supervisor` (true),
`autostart_supervisor` (false).

### `launch/high_level_stack.launch.py`

Слой экскурсий, общий для железа и симуляции: `guide_robot_voice`
(микрофон/динамик, ASR/VAD/wakeword, TTS) + `guide_robot_semantic_map`
(route_server + контент/локации/маршруты) + `guide_robot_mission_control`
(mission_fsm + narration_server + presence_monitor) + `guide_robot_face`
(HTTP+SVG лицо на DP-панели, не lifecycle). Voice/semantic_map/mission
поднимаются со своим `autostart:=false` — bring-up делает супервизор
(группы `voice`/`semantic_map`/`mission`,
`guide_robot_supervisor/config/supervisor.yaml`). Аргументы:
`use_sim_time` (false), `launch_voice`/`launch_semantic_map`/
`launch_mission`/`launch_face` (все true).

**Грабля (воспроизведено вживую):** `voice`/`semantic_map`/`mission`
каждый сам объявляет launch-аргумент `params_file` со своим дефолтом,
но `gazebo_ros/launch/gzserver.launch.py` ТОЖЕ объявляет `params_file`
(default `""`, для несвязанного `--params-file` у gzserver) — при
подключении вместе с `nav_stack.launch.py` (там `GroupAction(scoped=True)`
для нав2 корректно выставляет `params_file` внутри своей области, но
откатывает обратно на `""` при выходе из скоупа) общий (плоский!)
launch-контекст к моменту `high_level_stack.launch.py` уже содержит
`params_file=""`, и ноды получают `Path("")` == `Path(".")` →
`IsADirectoryError` при открытии как yaml. Исправлено — `params_file`
передаётся явно в каждый include (`SetLaunchConfiguration` всегда
перезаписывает, в отличие от `DeclareLaunchArgument`), плюс каждый
include завёрнут в свой `GroupAction` (scoped) — см. комментарии в
`high_level_stack.launch.py`.

### `launch/desk.launch.py`

Голос + semantic_map + mission + LLM **без** `ros2_control` / Nav2 /
лидаров. Когда реле моторов снято, `hardware.launch.py` падает на
драйвере — этот файл нет.

```bash
ros2 launch guide_robot_bringup desk.launch.py
```

`voice_jetson.yaml`, `autostart:=true` (супервизора нет). Тур из диалога
не поедет: `NavigateToPose` некому исполнить.

### `launch/view_robot.launch.py`

Только для визуальной проверки геометрии/TF URDF без реального робота
и без `ros2_control`: `robot_state_publisher` + `joint_state_publisher_gui`
(ползунки колёс) + RViz (`rviz/view_robot.rviz`, fixed frame
`base_footprint`). Аргумент: `gui` (true) — выключает
`joint_state_publisher_gui`.

### Консольные ноды (`guide_robot_bringup/*.py`)

- `laser_sector_blanker` — republish `LaserScan` с обнулёнными (`inf`)
  заданными угловыми секторами (параметры: `input_topic`,
  `output_topic`, `blind_sectors_deg` — строка `"lo,hi,lo,hi,..."`).
- `laser_blind_sector_finder` — офлайн-утилита калибровки: слушает
  топик скана заданное время, ищет угловые бины со стабильно близкой
  дальностью (кандидаты в «слепые» секторы крепления), печатает готовую
  строку `blind_sectors_deg` для вставки в `lidars.launch.py`.
- `scan_merger` — экспериментальный merge+deskew (Python). На железе
  **не запускается** (см. откат в `lidars.launch.py`): на Orin ~1.4 ядра.
  Unit-тесты математики — `test/test_scan_merge_math.py`.

## Известные проблемы

- `scan_merger` не поднимается ни одним launch: Python-deskew откачен
  2026-08-11. `dual_laser_merger` остаётся, пока deskew не перепишут на C++.
- Прямой `ros2 launch … nav_stack.launch.py` без обёртки теперь с
  `autostart_nav:=false` (контракт супервизора). Для отладки одного Nav2
  без супервизора нужно явно `autostart_nav:=true launch_supervisor:=false`.

## Зависимости

Из `package.xml` (`exec_depend`): `rclpy`, `sensor_msgs`, `tf2_ros`,
`python3-numpy`, `robot_state_publisher`, `guide_robot_hardware`,
`guide_robot_description`, `sllidar_ros2`, `dual_laser_merger`
(и железо, и симуляция), `foxglove_bridge`, `guide_robot_sonar`,
`slam_toolbox`, `guide_robot_navigation`, `rviz2`, `controller_manager`,
`joint_state_publisher_gui`, `guide_robot_supervisor`,
`guide_robot_simulation`, `guide_robot_mission_control`,
`guide_robot_voice`, `guide_robot_semantic_map`, `guide_robot_face`.
`test_depend`:
`ament_copyright`, `python3-pytest`.

Из `setup.py`: `entry_points.console_scripts` = `laser_sector_blanker`,
`laser_blind_sector_finder`, `scan_merger`; устанавливаются `launch/*.py`
и `rviz/*.rviz`.

## Диагностика задержки лидаров

Разбирает, из чего складывается запаздывание `/scan` и сколько градусов
оно стоит на развороте. Анализатор — `scripts/lidar_lag.py`.

Бланкер штамп сохраняет (переиздаёт то же сообщение). Штамп `/scan`
зависит от мерджера: `dual_laser_merger` наследует `header.stamp`
первого лидара, поэтому стадия `filtered -> /scan` джойнится точным
совпадением штампа — это основной путь. Второй путь, восстановление
пары по ближайшим штампам, нужен для мерджера с общим временем
(`scan_merger`, если вернётся) и включается сам, если точных
совпадений нет.

Поднимать стек **без Foxglove**: один лишний RELIABLE-читатель заметно
поднимает CPU всех C++ нод, а мост стоит ~1.36 ядра — с ним бэг измерит не
ту машину, которая работает в продакшене.

```bash
ros2 launch guide_robot_bringup hardware.launch.py launch_foxglove:=false

# в другом терминале, ~5 минут -> ~90 МБ
ros2 bag record -o lidar_lag_$(date -u +%Y%m%dT%H%M%SZ) \
  /scan_left /scan_right /scan_left_filtered /scan_right_filtered /scan \
  /tf /tf_static /odom /joint_states /diff_drive_controller/cmd_vel_unstamped
```

Профиль движения (рука на стопе, разгоны и повороты — телепом либо
`odom_test.py spin`):

1. **60 с полностью стоя.** База: тут вся задержка — чистый конвейер, без
   вклада движения. Отсюда же берётся распределение рассинхрона лидаров.
2. **Разворот на месте ~0.5 рад/с, 2 полных оборота.**
3. **Разворот на месте ~1.0 рад/с, 2 полных оборота.** Две скорости нужны,
   чтобы отделить постоянную задержку от ошибки, пропорциональной omega.
4. **30 с прямо 0.35 м/с** (в честном диапазоне привода).

```bash
python3 scripts/lidar_lag.py lidar_lag_<...>
python3 scripts/lidar_lag.py --self-check   # проверить разбор CDR
```

Чего ждать в отчёте и как читать:

- `/scan_left` относительно своего штампа — не меньше ~100 мс, и это не
  дефект: `sllidar_node.cpp:362` ставит штамп на **начало** развёртки, а
  публикует после её конца. Столько же уходит в систематический сдвиг
  привязки: содержимое скана центрировано на `stamp + 50 мс`.
- переход `/scan_left -> /scan_left_filtered` — цена питоновского бланкера.
  Замер его логики на x86 дал 0.28 мс медиану, так что здесь ожидаются
  единицы мс; если видно десятки — на Орине голодает исполнитель.
- переход `filtered(L,R) -> /scan` — чистая цена deskew'а + биннинга
  (ожидания партнёра, как у ApproximateTime, больше нет). Ожидаются
  единицы мс; если видно десятки — голодает Python на Орине.
- `|stamp_L - stamp_R|` — рассинхрон развёрток в паре. Без компенсации
  это был бы поворот одной половины `/scan` относительно другой; с
  deskew'ом уезжает только как нагрузка на TF-интерполяцию. В отчёте
  печатается, сколько градусов это СТОИЛО БЫ без компенсации.

Сам рекордер добавляет ~0.8 мс медианы (замерено по `/odom` и
`/joint_states` в бэге `20260811T121706Z`), но одиночные выбросы доходят до
~185 мс — колонки p95/макс читать с этой поправкой.

## Тестовый сценарий экскурсии (симуляция)

Проверено вживую в Gazebo от чистого `ros2 launch` до движущегося тура:

```bash
# Собрать хотя бы то, что менялось (interfaces -- первым, если трогали msg/srv/action)
colcon build --packages-select guide_robot_bringup guide_robot_voice \
    guide_robot_semantic_map guide_robot_mission_control guide_robot_supervisor
source install/setup.bash

# 1. Поднять всё: Gazebo + нав-стек + супервизор (autostart_supervisor:=true
#    по умолчанию в simulation.launch.py) + voice/semantic_map/mission
ros2 launch guide_robot_bringup simulation.launch.py
```

Супервизор сам, без единого ручного вызова, проходит все группы по
порядку (`safety → localization → navigation → voice → semantic_map →
mission`, `guide_robot_supervisor/config/supervisor.yaml`) и переходит в
`ACTIVE` — обычно занимает 30-60 секунд (AMCL должен успеть
залокализоваться на карте, чтобы прошёл precondition `tf_map` группы
`navigation`). Следить за прогрессом:

```bash
ros2 topic echo /supervisor/state
# ... INIT -> BRINGUP -> ACTIVE
```

Как только `state=ACTIVE`, в отдельном терминале (тот же
`ROS_DOMAIN_ID`/`source install/setup.bash`, что и в шаге 1):

```bash
# Список туров -- guide_robot_semantic_map/config/tours.yaml,
# демонстрационный (6 остановок) называется lab_demo
ros2 run guide_robot_mission_control mission_cli tour --tour lab_demo --no-confirm
```

`--no-confirm` пропускает `AWAITING_CONFIRM` между остановками (в v1 это
всё равно тестовый хук `~/submit_confirm`, реального ASR/LLM-подтверждения
нет — см. `guide_robot_mission_control/README.md`). CLI держит соединение
и печатает фидбек по мере тура:

```
тур принят, жду завершения (Ctrl+C -- отменить)...
[feedback] phase=GREETING stop=1/6 stop_id=entrance
[feedback] phase=NAVIGATING stop=1/6 stop_id=entrance
[feedback] phase=NARRATING stop=1/6 stop_id=entrance
...
outcome=COMPLETED stops_completed=6 stops_skipped=0
```

Ctrl+C на CLI обрывает только клиента -- сам тур на `mission_fsm`
продолжается (goal никуда не делся); статус в любой момент:

```bash
ros2 run guide_robot_mission_control mission_cli status --once
# state=NAVIGATING stop=2/6 stop_id=robo_guide exhibit_id=robo_guide resume_available=False presence=False
```

Остальные команды CLI (`pause [--hard]`, `resume`, `confirm yes|no`,
`say "текст"`, `barge`) — см. «CLI» в
`guide_robot_mission_control/README.md`.

**Если супервизор не дошёл до `ACTIVE`** (`ros2 topic echo
/supervisor/diagnostics` покажет, какая группа/watchdog застряли) —
самый частый случай в симуляции: AMCL не успел залокализоваться за
`precondition_timeout` (60 с по умолчанию) без начального `2D Pose
Estimate` в RViz. Обходной путь для тестирования БЕЗ супервизора --
поднять нав-стек и высокий слой напрямую с `autostart:=true`, минуя
`guide_robot_supervisor` совсем:

```bash
ros2 launch guide_robot_bringup nav_stack.launch.py autostart_nav:=true launch_supervisor:=false
ros2 launch guide_robot_bringup high_level_stack.launch.py autostart:=true
```
