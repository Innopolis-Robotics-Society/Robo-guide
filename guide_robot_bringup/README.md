# guide_robot_bringup

Пакет верхнеуровневой оркестрации запуска робота Guide-Robot (Guide Robot):
собирает воедино `ros2_control` (диффдрайв), два лидара RPLIDAR C1 со
слиянием сканов, сонары, Foxglove Bridge, стек Nav2 (AMCL или SLAM
Toolbox), супервизор lifecycle-нод (`guide_robot_supervisor`) и RViz.
Сам пакет не содержит "бизнес-логики" робота — только launch-файлы,
rviz-конфиги и две вспомогательные Python-ноды для калибровки лидаров.

Тип сборки — `ament_python`.

## Обзор

Слой запуска состоит из четырёх launch-файлов и двух консольных нод:

- на реальном роботе точка входа — `hardware.launch.py`;
- в симуляции (Gazebo) — `simulation.launch.py`;
- `sensors.launch.py` и `view_robot.launch.py` — самостоятельные
  вспомогательные launch-файлы (первый переиспользуется из
  `hardware.launch.py`, второй — только для просмотра URDF в RViz без
  `ros2_control`).

Управление жизненным циклом Nav2-нод формально идёт через
`nav2_lifecycle_manager` (внутри `guide_robot_navigation`) плюс
отдельный супервизор `guide_robot_supervisor`, который должен поэтапно
поднимать группы lifecycle-нод по precondition'ам (TF, частота
скана/сонаров и т.д.) — см. `guide_robot_supervisor/config/supervisor.yaml`.

## Файлы запуска

### `launch/hardware.launch.py`

Основной launch для реального робота. Поднимает:
`robot_state_publisher` - `ros2_control_node` (`controller_manager`) -
спаунеры `diff_drive_controller` и `joint_state_broadcaster` -
опционально `sensors.launch.py`, сонар (`guide_robot_sonar`,
`sonar_node_mult.py`), Foxglove Bridge - стек Nav2/SLAM
(`guide_robot_navigation`) - супервизор (`guide_robot_supervisor`,
безусловно) - опционально RViz2 с `rviz/hardware.rviz`.

Аргументы: `use_sim_time` (false), `use_mock_hardware` (false),
`launch_sensors` (true), `launch_sonar` (true), `launch_foxglove` (true),
`slam` (false), `slam_params_file`, `map`, `nav` (true), `nav_params_file`,
`launch_rviz` (true), `autostart_supervisor` (false), `autostart_nav` (false).

`autostart_supervisor:=false` оставляет супервизор в `INIT` — стек
поднимается только по вызову сервиса `/supervisor/bringup`; политики
watchdog'ов до этого не действуют. `autostart_nav` уходит в
`nav2_lifecycle_manager` и должен оставаться `false`, пока группами
управляет супервизор (см. `guide_robot_supervisor/config/supervisor.yaml`).

### `launch/sensors.launch.py`

Два `sllidar_ros2` (`sllidar_left`/`sllidar_right`, RPLIDAR C1,
460800 бод), с правым лидаром, задержанным на 5 с (`lidar_start_delay`)
во избежание просадки питания при одновременном старте. Каждый скан
проходит через `laser_sector_blanker` (свой пакетный exec) — вырезает
угловой сектор, где лидар видит собственное крепление / крепление
второго лидара (жёстко заданные `left/right_blind_sectors_deg`,
откалиброванные вручную через `laser_blind_sector_finder`), затем
`dual_laser_merger` сливает `/scan_left_filtered` + `/scan_right_filtered`
в единый `/scan` в кадре `base_footprint`.

Аргументы: `left_port` (`/dev/tty_lidar_left`), `right_port`
(`/dev/tty_lidar_right`), `baudrate`, `use_sim_time`, `merge_frame`,
`lidar_start_delay`.

### `launch/simulation.launch.py`

Точка входа для Gazebo-симуляции: `gazebo.launch.py` из
`guide_robot_simulation`, `dual_laser_merger` (без калибровочных
офсетов — предполагается, что TF в симуляции точная),
SLAM Toolbox или Nav2 (`guide_robot_navigation`), супервизор
(`guide_robot_supervisor`, только при `slam:=false`), RViz с
`rviz/sim.rviz`. Аргументы: `slam` (False), `map`, `rviz` (true),
`nav_params`, `slam_params`.

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
  строку `blind_sectors_deg` для вставки в `sensors.launch.py`.

## Зависимости

Из `package.xml` (`exec_depend`): `rclpy`, `sensor_msgs`,
`robot_state_publisher`, `guide_robot_hardware`, `guide_robot_description`,
`sllidar_ros2`, `dual_laser_merger`, `foxglove_bridge`, `guide_robot_sonar`,
`slam_toolbox`, `guide_robot_navigation`, `rviz2`, `controller_manager`,
`joint_state_publisher_gui`, `guide_robot_supervisor`,
`guide_robot_simulation`. `test_depend`: `ament_copyright`, `python3-pytest`.

Из `setup.py`: `entry_points.console_scripts` = `laser_sector_blanker`,
`laser_blind_sector_finder`; устанавливаются `launch/*.py` и `rviz/*.rviz`.
