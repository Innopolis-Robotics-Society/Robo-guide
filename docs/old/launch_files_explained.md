# Launch-файлы в ROS 2 — Полное объяснение

## 1. Что такое launch-файл?

Launch-файл — это Python-скрипт, который описывает **какие узлы запустить, с какими параметрами
и в каком порядке**. ROS 2 ищет в нём одну обязательную функцию:

```python
def generate_launch_description() -> LaunchDescription:
    ...
```

`LaunchDescription` — это контейнер со списком "действий" (Actions). ROS 2 выполняет их при запуске.

---

## 2. Основные строительные блоки

### 2.1 Node — запуск узла

```python
from launch_ros.actions import Node

node = Node(
    package='my_package',        # имя ROS 2 пакета
    executable='my_executable',  # имя из console_scripts в setup.py
    name='my_node_name',         # (опц.) переопределить имя узла
    namespace='robot1',          # (опц.) пространство имён → /robot1/cmd_vel
    parameters=[                 # (опц.) параметры узла
        {'param_name': value},
        '/path/to/params.yaml',  # или YAML-файл
    ],
    remappings=[                 # (опц.) переименовать топики
        ('/cmd_vel', '/robot/cmd_vel'),
    ],
    output='screen',             # (опц.) выводить логи в терминал
    arguments=['--ros-args'],    # (опц.) доп. аргументы командной строки
)
```

### 2.2 DeclareLaunchArgument — объявить аргумент

Аргументы — это **переменные**, которые пользователь может передать при запуске:
```bash
ros2 launch my_package my.launch.py use_sim_time:=true
```

```python
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

# 1. Объявляем аргумент (имя, дефолт, описание)
declare_sim_time = DeclareLaunchArgument(
    name='use_sim_time',
    default_value='false',
    description='Use simulation clock if true'
)

# 2. Читаем значение аргумента в коде
use_sim_time = LaunchConfiguration('use_sim_time')

# 3. Используем в узле
node = Node(
    package='my_pkg',
    executable='my_node',
    parameters=[{'use_sim_time': use_sim_time}]
)
```

> **Важно:** `LaunchConfiguration` — это не строка! Это объект-подстановка (Substitution).
> ROS 2 вычислит его значение только в момент запуска, не раньше.

### 2.3 IncludeLaunchDescription — включить другой launch-файл

```python
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from ament_index_python.packages import get_package_share_directory
import os

# Включить nav2_bringup/launch/navigation.launch.py
nav2_launch = IncludeLaunchDescription(
    PythonLaunchDescriptionSource(
        os.path.join(get_package_share_directory('nav2_bringup'), 'launch', 'navigation.launch.py')
    ),
    launch_arguments={
        'use_sim_time': 'false',
        'map': '/path/to/map.yaml',
    }.items()
)
```

### 2.4 GroupAction — группировка с namespace

```python
from launch.actions import GroupAction
from launch_ros.actions import PushRosNamespace

group = GroupAction([
    PushRosNamespace('robot1'),  # все узлы внутри → /robot1/...
    Node(package='...', executable='...'),
    Node(package='...', executable='...'),
])
```

### 2.5 ExecuteProcess — запустить произвольную команду

```python
from launch.actions import ExecuteProcess

rviz = ExecuteProcess(
    cmd=['rviz2', '-d', '/path/to/config.rviz'],
    output='screen'
)
```

### 2.6 TimerAction — запустить с задержкой

```python
from launch.actions import TimerAction

delayed_node = TimerAction(
    period=3.0,  # секунды
    actions=[Node(package='...', executable='...')]
)
```

### 2.7 OpaqueFunction — выполнить произвольный Python-код

```python
from launch.actions import OpaqueFunction

def launch_setup(context, *args, **kwargs):
    # Здесь можно читать LaunchConfiguration как обычную строку
    value = LaunchConfiguration('my_arg').perform(context)
    # ... логика
    return [Node(...)]

my_action = OpaqueFunction(function=launch_setup)
```

---

## 3. Substitutions — подстановки

Поскольку аргументы вычисляются лениво, используются специальные объекты:

| Substitution | Что делает |
|---|---|
| `LaunchConfiguration('arg')` | Значение объявленного аргумента |
| `PathJoinSubstitution([pkg, 'urdf', 'robot.xacro'])` | Соединяет пути |
| `FindPackageShare('my_pkg')` | Путь к `share/my_pkg` |
| `Command(['xacro ', path])` | Выполняет команду, результат = stdout |
| `TextSubstitution(text='hello')` | Просто строка |
| `EnvironmentVariable('HOME')` | Переменная окружения |

Пример с `Command` (альтернатива `xacro.process_file`):
```python
from launch.substitutions import Command, FindPackageShare, PathJoinSubstitution

robot_description = Command([
    'xacro ',
    PathJoinSubstitution([FindPackageShare('guide_robot_description'), 'urdf', 'guide_robot_urdf.xacro'])
])
```

---

## 4. Полный шаблон с объяснением

```python
import os
import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():

    # ─── Аргументы ────────────────────────────────────────────────────────
    # Объявляем аргументы, которые можно передать при запуске:
    #   ros2 launch guide_robot_bringup hardware.launch.py use_sim_time:=true

    declare_use_sim_time = DeclareLaunchArgument(
        name='use_sim_time',
        default_value='false',
        description='Use simulation clock'
    )

    use_sim_time = LaunchConfiguration('use_sim_time')

    # ─── Получить URDF ────────────────────────────────────────────────────
    # get_package_share_directory → находит путь к установленному пакету
    # (после colcon build он будет в install/guide_robot_description/share/...)

    urdf_path = os.path.join(
        get_package_share_directory('guide_robot_description'),
        'urdf',
        'guide_robot_urdf.xacro'
    )

    # xacro.process_file → компилирует .xacro в обычный .xml (строку)
    robot_description = xacro.process_file(urdf_path).toxml()

    # ─── Узлы ─────────────────────────────────────────────────────────────

    # robot_state_publisher:
    #   - читает параметр robot_description (URDF-строку)
    #   - публикует статические TF (base_link → sonar_1_link и т.д.)
    #   - публикует /joint_states
    robot_state_publisher_node = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        output='screen',
        parameters=[{
            'robot_description': robot_description,
            'use_sim_time': use_sim_time,
        }]
    )

    # Наш драйвер шасси:
    #   - подписывается на /cmd_vel
    #   - публикует /odom и TF odom→base_link
    motor_driver_node = Node(
        package='guide_robot_hardware',
        executable='motor_driver',
        output='screen',
        parameters=[{
            'use_sim_time': use_sim_time,
        }]
    )

    # ─── Собрать LaunchDescription ────────────────────────────────────────
    # Порядок важен: сначала DeclareLaunchArgument, потом всё остальное!

    return LaunchDescription([
        declare_use_sim_time,
        robot_state_publisher_node,
        motor_driver_node,
    ])
```

---

## 5. Как запустить

```bash
# Сначала собрать проект
cd ~/Robo-guide
colcon build --symlink-install
source install/setup.bash

# Запустить
ros2 launch guide_robot_bringup hardware.launch.py

# Или с аргументом:
ros2 launch guide_robot_bringup hardware.launch.py use_sim_time:=true
```

---

## 6. Типичные ошибки

| Ошибка | Причина |
|---|---|
| `Package 'X' not found` | Не сделал `source install/setup.bash` после `colcon build` |
| `No executable found` | Неверное имя `executable` (должно совпадать с `console_scripts`) |
| `glob('launch/*.py')` вернул `[]` | Launch-файл не в папке `launch/` или не заканчивается на `.py` |
| Параметры не применяются | Забыл добавить файл в `data_files` в `setup.py` |
