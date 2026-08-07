# guide_robot_description

## Обзор

Пакет содержит URDF/xacro-описание робота-гида Guide-Robot, `ros2_control`-интерфейс
базы, единственный источник физических параметров робота (`config/robot_params.yaml`)
и шаблонизатор `scripts/render_params.py`, которым конфиги других пакетов
(включая `guide_robot_navigation`) подставляют числа из этого файла в себя.
Своей логики управления, сенсорных нод или навигации пакет не содержит — это
чисто описательный + конфиг-генерирующий пакет, `ament_cmake`, без Python-модулей.

Состав:

```
config/
  robot_params.yaml        # физические параметры робота (источник истины)
  controllers.yaml.in      # шаблон для diff_drive_controller (${section.key})
meshes/
  al_profile.stl           # соединительный профиль 2020 между лидарными стойками
  lidars_mount.stl         # площадка крепления лидара
scripts/
  render_params.py         # шаблонизатор ${section.key} -> значение из YAML
urdf/
  guide_robot.urdf.xacro         # links/joints, читает robot_params.yaml напрямую
  guide_robot.ros2_control.xacro # <ros2_control> блок, 3 бэкенда
  guide_robot.gazebo.xacro       # Gazebo Classic: плагины, лидары, IMU, сонары
```

Сборка (`CMakeLists.txt:13-26`) на этапе `colcon build` рендерит
`config/controllers.yaml` из `controllers.yaml.in` и параметров
`robot_params.yaml`, и ставит `urdf/`, `meshes/`, `robot_params.yaml` и
сгенерированный `controllers.yaml` в `share/guide_robot_description/...`.
Сам `render_params.py` дополнительно ставится как исполняемый файл в
`lib/guide_robot_description/`, но фактически это не используется — ни один
launch-файл его оттуда не запускает (см. «Известные проблемы»).

## Модель робота

Геометрия — из `RobotSetting.xml`/`config.txt` реального Guide-Robot, перепроверена
рулеткой/прокатыванием и записана в `robot_params.yaml` (раздел `geometry`).
Меши подключены только там, где это дёшево для costmap; корпус и колёса —
примитивы.

- **base_footprint → base_link** (`urdf.xacro:50-78`): `base_footprint` без
  геометрии, `base_link` — цилиндр (`body_radius`/`body_height`), соединён
  фиксированным joint'ом со смещением по Z на `wheel_radius` (0.1026 м). Визуал
  и коллизия у `base_link` смещены на X=-0.12 (капот/спина корпуса относительно
  оси колёс) — **это число захардкожено** трижды (см. ниже), хотя в
  `robot_params.yaml:12` для него заведён параметр `body_x_offset`.
- **Колёса** (`urdf.xacro:81-111`): макрос `wheel`, два continuous joint'а
  (`left_wheel_joint`, `right_wheel_joint`) вокруг оси Y, радиус/ширина из
  `geometry.wheel_radius`/`wheel_width`. Эти же имена joint'ов используются в
  `guide_robot.ros2_control.xacro`, `guide_robot.gazebo.xacro` и в
  `guide_robot_hardware` (протокол опроса энкодеров/скоростей).
- **Задние каретки-опоры** (`urdf.xacro:119-149`): реальная часть шасси,
  смоделированы сферами радиуса `wheel_radius` на фиксированных joint'ах,
  в Gazebo — нулевое трение (`gazebo.xacro:140-153`), чтобы не искажали
  одометрию diff-drive.
- **Сонары** (`urdf.xacro:152-180`): 7 фиксированных линков
  `sonar_sensor_{1,2,4,5,6,8,9}` на `base_link`. Нумерация не по порядку —
  это исходные ID из проводки Guide-Robot, а не порядок опроса. Те же имена
  фреймов использует `guide_robot_sonar/scripts/sonar_node_mult.py:136-144`
  (маппинг ID драйвера → frame_id) — фреймы согласованы между пакетами.
- **Лидарные стойки** (`urdf.xacro:185-245`): два `*_lidar_mount_link` (меш
  `lidars_mount.stl`) + `lidar_bar_link` (меш `al_profile.stl`, соединительный
  профиль между стойками) — все фиксированы на `base_link`.
- **Лидарные фреймы** (`urdf.xacro:255-267`): `laser_frame_left`/`laser_frame_right`
  висят на соответствующих `*_lidar_mount_link`, повёрнуты на `pi` по pitch —
  реальные лидары стоят перевёрнуто и "задом наперёд", поворот вокруг Y разом
  переворачивает X (вперёд/назад) и Z (верх/низ), не трогая Y. Используются в
  `guide_robot.gazebo.xacro:34-102` как `frame_name` для двух лидар-сенсоров
  (`/scan_left`, `/scan_right`), которые потом сливает `dual_laser_merger`
  (см. `guide_robot_simulation/launch/gazebo.launch.py`).
- **IMU** (`urdf.xacro:271-276`): фиксированный `imu_link` на `base_link`,
  позиция отмечена как непроверенная (TO-DO).

Материалы (`grey`/`dark`/`blue`) заданы только для RViz; для Gazebo — отдельно
через `<gazebo reference=...><material>` в `guide_robot.gazebo.xacro`.

## ros2_control интерфейс

`guide_robot.ros2_control.xacro` не самодостаточен: использует свойства `p`,
`wheel_separation`, `wheel_radius`, объявленные в `guide_robot.urdf.xacro` до
`xacro:include` — обрабатывать этот файл отдельно нельзя (см. комментарий
`ros2_control.xacro:10-12`).

`<ros2_control name="GuideRobotSystem" type="system">` с тремя
взаимоисключающими hardware-плагинами, выбираемыми аргументами `use_sim` /
`use_mock_hardware` (`ros2_control.xacro:47-79`):

1. `use_sim:=true` → `gazebo_ros2_control/GazeboSystem` (симуляция).
2. `use_mock_hardware:=true` → `mock_components/GenericSystem` (заглушка для
   прогона стека без железа, `calculate_dynamics=true`).
3. иначе → `guide_robot_hardware/GuideRobotSystem` — реальный Dynamixel-драйвер
   базы, получает через `<param>` весь раздел `drive` из `robot_params.yaml`
   (`serial_port`, `baud_rate`, ID моторов, `ticks_per_rev`, знаки направления,
   `speed_coefficient`, `cmd_timeout`) плюс `wheel_radius` из `geometry`.
   Дефолты в `guide_robot_hardware/include/.../guide_robot_system.hpp:93-100`
   совпадают с текущими значениями `robot_params.yaml` — это дублирование
   значений в двух местах, но не рассинхронизировано на момент проверки.

Интерфейсы на оба колёсных joint'а одинаковые (`wheel_iface`,
`ros2_control.xacro:36-45`): `command_interface velocity` с границами
`[-w_wheel_max, w_wheel_max]`, `state_interface position` и `velocity`.
`w_wheel_max` — не физический лимит, а вычисляемый запас command_interface
(худший случай линейная+угловая скорость одновременно, с запасом
`wheel_limit_margin`); реальное ограничение делает `diff_drive_controller`
по параметрам `linear.x.*`/`angular.z.*` в `controllers.yaml.in`.

## Параметры/скрипты

`robot_params.yaml` — единственный источник физических констант робота,
секции: `geometry`, `odometry` (калибровочные множители одометрии),
`limits`, `drive` (порт/ID/знаки Dynamixel-моторов), `sensors`,
`controller_manager`. Значения активно прокомментированы: указана методика
измерения и дата (`2026-07-26`/`27`), где параметр обмерян заново взамен
неверного заводского `RobotSetting.xml`.

Файл используется **двумя независимыми механизмами**:

1. `guide_robot.urdf.xacro:20-37` читает его напрямую через встроенный
   `xacro.load_yaml()` — так в URDF попадает геометрия.
2. `render_params.py` (`${section.key}` → значение, с проверкой "все
   отсутствующие ключи разом одной ошибкой сборки", без try/except вокруг
   `yaml.safe_load`/открытия файлов) — так рендерятся `*.yaml.in`-шаблоны.
   Этим механизмом пользуется:
   - `CMakeLists.txt:13-24` этого же пакета — `controllers.yaml.in` →
     `config/controllers.yaml` при `colcon build`;
   - `guide_robot_navigation/setup.py:14-32` — **импортирует
     `render_params.py` из исходников `guide_robot_description` через
     `importlib`** (не как ROS-зависимость и не как инсталлированный модуль)
     и рендерит свои `config/*.in` при сборке своего пакета.

   Это подтверждает: `guide_robot_description` — источник парсера
   `robot_params.yaml`, упомянутого в коммите «feat: add correct
   robot_params.yaml parsers», а не потребитель чужого.

В самом `robot_params.yaml` в конце (строки 77-165) лежит большой блок
`old:` — архивные значения из `RobotSetting.xml`/`config.txt` с комментариями
по методике измерения. Ни `render_params.py`, ни `xacro.load_yaml()`-обращения
в него не заглядывают (нет ни одного обращения к ключу `old.*`) — это чисто
справочный текст, физически не влияющий на сборку.

## Известные проблемы и замечания

- **`render_params.py` не обрабатывает ошибки формата.** Открытие файлов и
  `yaml.safe_load` (`render_params.py:35-36`) — без `try/except`, ошибка
  формата уронит сборку сырым traceback'ом Python, а не понятным
  сообщением (в отличие от продуманной проверки отсутствующих ключей на
  `render_params.py:41-51`). `as_yaml_scalar()` (`:26-31`) безусловно берёт
  строки в одинарные кавычки — значение с апострофом внутри сломает
  сгенерированный YAML; сейчас таких значений нет, но защиты от них тоже нет.