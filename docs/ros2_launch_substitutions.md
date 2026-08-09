# ROS2 Launch: Substitutions и поиск файлов

## 1. Две фазы выполнения launch-файла

Когда ты запускаешь:
```bash
ros2 launch my_pkg hardware.launch.py use_mock_hardware:=true
```

Происходит **две отдельные фазы**:

```
╔══════════════════════════════════════════════════════════════╗
║  ФАЗА 1: Python выполняется                                  ║
║  generate_launch_description() вызывается                    ║
║  Создаётся "дерево" объектов (LaunchDescription, Node...)    ║
║                                                              ║
║  ⚠️  Аргументы командной строки ЕЩЁ НЕ ПРОЧИТАНЫ            ║
║  ⚠️  use_mock_hardware=??? (неизвестно на этом этапе)        ║
╚══════════════════════════════════════════════════════════════╝
                          ↓
╔══════════════════════════════════════════════════════════════╗
║  ФАЗА 2: ros2 launch обходит дерево объектов                 ║
║  Вычисляет Substitution-объекты → получает реальные строки   ║
║  Запускает процессы с готовыми параметрами                   ║
║                                                              ║
║  ✅  use_mock_hardware = "true"  (аргумент уже известен)     ║
╚══════════════════════════════════════════════════════════════╝
```

---

## 2. Что такое Substitution

**Substitution** — это объект-"обещание". Он не хранит строку, а хранит **инструкцию о том, как получить строку позже**, в Фазе 2.

```python
# Это НЕ строка — это объект типа LaunchConfiguration
use_mock = LaunchConfiguration("use_mock_hardware")
print(use_mock)  # → <LaunchConfiguration 'use_mock_hardware'>
#                    Строку не получишь через print!
```

Все классы из `launch.substitutions` и `launch_ros.substitutions` — это Substitution:
- `LaunchConfiguration(name)` — значение аргумента командной строки
- `PathJoinSubstitution([...])` — соединение частей пути
- `FindPackageShare(pkg)` — путь к share/ пакета ROS
- `FindExecutable(name)` — путь к исполняемому файлу
- `Command([...])` — результат выполнения команды в терминале

---

## 3. Сравнение всех подходов поиска файлов

### 3.1. `os.path.join` + `get_package_share_directory`

```python
import os
from ament_index_python.packages import get_package_share_directory

path = os.path.join(
    get_package_share_directory("guide_robot_description"),
    "urdf",
    "robot.urdf.xacro"
)
# path — обычная строка: "/opt/ros/humble/share/guide_robot_description/urdf/robot.urdf.xacro"
```

| Свойство | Значение |
|---|---|
| Тип результата | Обычная строка Python |
| Когда вычисляется | Фаза 1 (сразу при импорте) |
| Может содержать LaunchConfiguration? | ❌ Нет |
| Где применять | Статичные пути, не зависящие от аргументов |

**Когда использовать:** путь к файлу который всегда одинаков, независимо от аргументов запуска.

---

### 3.2. `PathJoinSubstitution` + `FindPackageShare`

```python
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare

path = PathJoinSubstitution([
    FindPackageShare("guide_robot_description"),
    "urdf",
    "robot.urdf.xacro"
])
# path — это Substitution-объект, ещё не строка
```

| Свойство | Значение |
|---|---|
| Тип результата | Substitution объект |
| Когда вычисляется | Фаза 2 (при запуске) |
| Может содержать LaunchConfiguration? | ✅ Да |
| Где применять | Везде как best practice, особенно если путь зависит от аргумента |

**Внутренняя работа:**
```
FindPackageShare("guide_robot_description")
    → в Фазе 2 вызывает ament_index и находит: "/opt/ros/humble/share/guide_robot_description"

PathJoinSubstitution([часть1, "urdf", "robot.urdf.xacro"])
    → соединяет всё через os.sep: "/opt/ros/humble/share/guide_robot_description/urdf/robot.urdf.xacro"
```

---

### 3.3. `Command` + `FindExecutable`

```python
from launch.substitutions import Command, FindExecutable, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare

urdf_path = PathJoinSubstitution([
    FindPackageShare("guide_robot_description"), "urdf", "robot.urdf.xacro"
])
use_mock = LaunchConfiguration("use_mock_hardware")

robot_description = Command([
    FindExecutable(name="xacro"),   # → "/usr/bin/xacro" (найденный исполняемый файл)
    " ",
    urdf_path,                      # → "/opt/.../robot.urdf.xacro"
    " use_mock_hardware:=",
    use_mock,                       # → "true" или "false" (из аргументов)
])
```

**В Фазе 2 это превращается в вызов команды:**
```bash
/usr/bin/xacro /opt/ros/humble/share/.../robot.urdf.xacro use_mock_hardware:=true
```

И результат (XML-строка от xacro) становится значением `robot_description`.

| Свойство | Значение |
|---|---|
| Тип результата | Substitution (строка — результат команды) |
| Когда вычисляется | Фаза 2, запускает реальный subprocess |
| Может содержать LaunchConfiguration? | ✅ Да — поэтому мы так и делаем |
| Где применять | Когда нужно передать аргументы в xacro |

---

## 4. Почему `xacro.process_file()` не работал

```python
# НЕПРАВИЛЬНО:
use_mock = LaunchConfiguration("use_mock_hardware")  # Substitution-объект
path = os.path.join(get_package_share_directory("pkg"), "robot.urdf.xacro")

robot_description = xacro.process_file(path).toxml()
# ↑ Выполняется в Фазе 1
# xacro обрабатывает файл БЕЗ аргумента use_mock_hardware
# Аргумент командной строки ещё не существует → xacro использует default из файла
```

```python
# ПРАВИЛЬНО:
use_mock = LaunchConfiguration("use_mock_hardware")  # Substitution-объект

robot_description = Command([
    FindExecutable(name="xacro"), " ", urdf_path,
    " use_mock_hardware:=", use_mock  # ← передаём Substitution
])
# ↑ Создаётся только "инструкция"
# В Фазе 2 use_mock уже = "true", и xacro получает правильный аргумент
```

---

## 5. Итоговая таблица

| Инструмент | Фаза | Тип | Может использовать LaunchConfiguration? | Применение |
|---|---|---|---|---|
| `os.path.join` | 1 | строка | ❌ | Статичные пути |
| `get_package_share_directory` | 1 | строка | ❌ | Статичные пути |
| `FindPackageShare` | 2 | Substitution | ✅ | Динамичные пути |
| `PathJoinSubstitution` | 2 | Substitution | ✅ | Соединение частей пути |
| `FindExecutable` | 2 | Substitution | ✅ | Поиск команды в PATH |
| `Command` | 2 | Substitution | ✅ | Запуск команды, получение вывода |
| `LaunchConfiguration` | 2 | Substitution | — | Чтение аргумента командной строки |
| `DeclareLaunchArgument` | 1 | Action | — | Объявление аргумента (не значение!) |

---

## 6. Частая ошибка: путать `DeclareLaunchArgument` и `LaunchConfiguration`

```python
# DeclareLaunchArgument — ОБЪЯВЛЯЕТ аргумент (говорит "такой аргумент существует")
declare_mock = DeclareLaunchArgument(name="use_mock_hardware", default_value="false")

# LaunchConfiguration — ЧИТАЕТ значение аргумента
use_mock_hardware = LaunchConfiguration("use_mock_hardware")

# В Command нужно использовать LaunchConfiguration, не DeclareLaunchArgument!
robot_description = Command([
    ...,
    " use_mock_hardware:=", use_mock_hardware,  # ✅ читаем значение
    # " use_mock_hardware:=", declare_mock,     # ❌ передаём сам объект-объявление
])

# И оба должны быть в LaunchDescription:
return LaunchDescription([
    declare_mock,          # ← регистрируем аргумент
    ...                    # ← use_mock_hardware нигде не добавляем, это не Action
])
```
