# guide_robot_bringup

Пакет верхнеуровневой оркестрации запуска робота Guide-Robot (Guide Robot):
собирает воедино `ros2_control` (диффдрайв), два лидара RPLIDAR C1 со
слиянием сканов, сонары, Foxglove Bridge, стек Nav2 (AMCL или SLAM
Toolbox), супервизор lifecycle-нод (`guide_robot_supervisor`) и RViz.
Сам пакет не содержит "бизнес-логики" робота — только launch-файлы,
rviz-конфиги и две вспомогательные Python-ноды для калибровки лидаров.

Тип сборки — `ament_python`.

## Обзор

Слой запуска состоит из пяти launch-файлов и двух консольных нод:

- на реальном роботе точка входа — `hardware.launch.py`;
- в симуляции (Gazebo) — `simulation.launch.py`;
- `sensors.launch.py` и `view_robot.launch.py` — самостоятельные
  вспомогательные launch-файлы (первый переиспользуется из
  `hardware.launch.py`, второй — только для просмотра URDF в RViz без
  `ros2_control`);
- `test.launch.py` — ручной сценарий для тестирования на стенде.

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
`sonar_node_mult.py`), Foxglove Bridge - через 10 секунд (`TimerAction`)
стек Nav2/SLAM (`guide_robot_navigation`) - опционально RViz2.

Аргументы: `use_sim_time` (false), `use_mock_hardware` (false),
`launch_sensors` (true), `launch_sonar` (true), `launch_foxglove` (true),
`slam` (false), `slam_params_file`, `map`, `nav` (true), `nav_params_file`,
`launch_rviz` (true), `autostart` (false), `autostart_nav` (false).

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
офсетов — предполагается, что TF в симуляции точная), через 10 с —
SLAM Toolbox или Nav2 (`guide_robot_navigation`), супервизор
(`guide_robot_supervisor`, только при `slam:=false`), RViz с
`rviz/sim.rviz`. Аргументы: `slam` (False), `map`, `rviz` (true),
`nav_params`, `slam_params`.

### `launch/test.launch.py`

Комбинирует `hardware.launch.py` + `sensors.launch.py` + отдельная
нода сонара (`sonar_node.py`) + отдельный `foxglove_bridge` — **см. п. 5
в известных проблемах**: с параметрами по умолчанию это приводит к
двойному запуску сенсоров/сонара/Foxglove.

### `launch/view_robot.launch.py`

Только для визуальной проверки геометрии/TF URDF без реального робота
и без `ros2_control`: `robot_state_publisher` + `joint_state_publisher_gui`
(ползунки колёс) + RViz (`rviz/view_robot.rviz`). Аргумент: `gui` (true,
не используется дальше в файле, см. «Известные проблемы»).

### Консольные ноды (`guide_robot_bringup/*.py`)

- `laser_sector_blanker` — republish `LaserScan` с обнулёнными (`inf`)
  заданными угловыми секторами (параметры: `input_topic`,
  `output_topic`, `blind_sectors_deg` — строка `"lo,hi,lo,hi,..."`).
- `laser_blind_sector_finder` — офлайн-утилита калибровки: слушает
  топик скана заданное время, ищет угловые бины со стабильно близкой
  дальностью (кандидаты в «слепые» секторы крепления), печатает готовую
  строку `blind_sectors_deg` для вставки в `sensors.launch.py`.

## Зависимости

Из `package.xml` (`exec_depend`): `rclpy`, `sensor_msgs`, `topic_tools`,
`robot_state_publisher`, `guide_robot_hardware`, `guide_robot_description`,
`sllidar_ros2`, `dual_laser_merger`, `foxglove_bridge`, `guide_robot_sonar`,
`slam_toolbox`, `guide_robot_navigation`. `test_depend`: `ament_copyright`,
`python3-pytest`.

Из `setup.py`: `entry_points.console_scripts` = `laser_sector_blanker`,
`laser_blind_sector_finder`; устанавливаются `launch/*.py`, `rviz/*.rviz`,
`config/*.yaml` (каталога `config/` в дереве сейчас нет — см. п. 1).

См. «Известные проблемы», п. 6 — список пакетов, фактически
используемых в launch-файлах, но не объявленных в `package.xml`.
