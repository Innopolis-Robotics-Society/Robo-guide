# ros2_control — Полное объяснение

## 1. Зачем нужен ros2_control?

Без ros2_control (как мы делаем сейчас):
- Ты сам пишешь узел, который читает `/cmd_vel`
- Сам считаешь кинематику
- Сам общаешься с железом (Serial/CAN)
- Сам публикуешь `/odom` и TF

Это **работает**, но у такого подхода есть минусы:
- Каждый раз для нового робота — пишешь заново
- Нет стандартного интерфейса → Nav2 и MoveIt не знают, как с тобой говорить
- Нет стандартных safety-механизмов (лимиты скорости, аварийная остановка)
- Сложно переключаться между симуляцией и реальным железом

**ros2_control** — это фреймворк, который стандартизирует всё это.
Он реализует паттерн **"разделение команд и исполнения"** (Controller — Hardware).

---

## 2. Архитектура: три уровня

```
┌─────────────────────────────────────────────────────┐
│                  УРОВЕНЬ 1: Контроллеры              │
│                                                      │
│  diff_drive_controller    joint_trajectory_controller│
│  (Nav2 посылает /cmd_vel) (MoveIt посылает команды)  │
└──────────────────┬──────────────────────────────────┘
                   │  (читает/пишет через интерфейсы)
┌──────────────────▼──────────────────────────────────┐
│           УРОВЕНЬ 2: Controller Manager              │
│                                                      │
│  Менеджер: загружает контроллеры, управляет жизн.   │
│  циклом, предоставляет общий буфер данных (State/   │
│  Command interfaces)                                 │
└──────────────────┬──────────────────────────────────┘
                   │  (читает/пишет в hardware abstractions)
┌──────────────────▼──────────────────────────────────┐
│        УРОВЕНЬ 3: Hardware Interface                 │
│                                                      │
│  Твой код (C++): читает энкодеры, отправляет        │
│  команды на моторы через Serial/CAN                  │
└─────────────────────────────────────────────────────┘
```

---

## 3. Ключевые концепции

### 3.1 Hardware Interface (Аппаратный интерфейс)
Это **твой** код на C++, который:
- `read()` — считывает данные с железа (скорости энкодеров)
- `write()` — отправляет команды на железо (скорости моторов)

Ты реализуешь **один класс**, который наследуется от `SystemInterface` (или `ActuatorInterface`).

```cpp
// Упрощённо — что ты реализуешь
class MyRobotHardware : public hardware_interface::SystemInterface {
    CallbackReturn on_init(const HardwareInfo & info) override;
    // вызывается в цикле управления:
    return_type read(const rclcpp::Time &, const rclcpp::Duration &) override;
    return_type write(const rclcpp::Time &, const rclcpp::Duration &) override;
};
```

### 3.2 State Interfaces и Command Interfaces
Это **стандартизированные каналы данных** между уровнями.

**State Interfaces** — данные С железа (только чтение):
```
joint_name/position   → текущий угол (рад)
joint_name/velocity   → текущая скорость (рад/с)
```

**Command Interfaces** — команды НА железо (запись):
```
joint_name/velocity   → желаемая скорость (рад/с)
```

Ты объявляешь их в URDF/XACRO, а ros2_control создаёт "переменные-трубы":
```
[Контроллер] --write--> [command_interface/velocity] --read--> [Hardware write()]
[Hardware read()] --write--> [state_interface/velocity] --read--> [Контроллер]
```

### 3.3 Controller Manager
- Загружает Hardware Interface и Контроллеры
- Запускает **жёсткий цикл управления** (control loop, обычно 100-1000 Гц)
- Порядок в каждом цикле: `read()` → контроллеры обновляют команды → `write()`

### 3.4 Контроллеры (готовые)
Это уже написанные узлы из пакета `ros2_controllers`:

| Контроллер | Что делает |
|---|---|
| `diff_drive_controller` | Принимает `/cmd_vel`, считает кинематику, публикует `/odom` и TF |
| `joint_trajectory_controller` | Управление манипулятором по траектории |
| `joint_state_broadcaster` | Публикует `/joint_states` (нужно для robot_state_publisher) |
| `velocity_controllers/JointGroupVelocityController` | Управление скоростью группы сочленений |

**Ключевое:** `diff_drive_controller` уже делает всё то, что ты сейчас пишешь вручную
(кинематику, одометрию, TF). Тебе нужно только написать Hardware Interface.

---

## 4. Как это описывается: ros2_control в URDF

В URDF добавляется специальный тег `<ros2_control>`:

```xml
<ros2_control name="my_robot" type="system">
  <!-- Указываем какой Hardware Interface загружать -->
  <hardware>
    <plugin>my_robot_hardware/MyRobotHardware</plugin>
    <!-- Параметры для твоего класса -->
    <param name="serial_port">/dev/ttyUSB0</param>
    <param name="baud_rate">115200</param>
  </hardware>

  <!-- Описываем сочленения (joints) -->
  <joint name="left_wheel_joint">
    <command_interface name="velocity"/>   <!-- можем задавать скорость -->
    <state_interface name="velocity"/>     <!-- можем читать скорость -->
    <state_interface name="position"/>     <!-- можем читать положение -->
  </joint>

  <joint name="right_wheel_joint">
    <command_interface name="velocity"/>
    <state_interface name="velocity"/>
    <state_interface name="position"/>
  </joint>
</ros2_control>
```

---

## 5. Как это всё запускается: launch-файл

```python
# В launch-файле:

# 1. robot_state_publisher (URDF → TF статика)
robot_state_publisher = Node(package='robot_state_publisher', ...)

# 2. controller_manager (главный менеджер)
controller_manager = Node(
    package='controller_manager',
    executable='ros2_control_node',
    parameters=[robot_description, controllers_config_yaml]
)

# 3. Запуск конкретных контроллеров
diff_drive = Node(
    package='controller_manager',
    executable='spawner',
    arguments=['diff_drive_controller']
)

joint_state_broadcaster = Node(
    package='controller_manager',
    executable='spawner',
    arguments=['joint_state_broadcaster']
)
```

---

## 6. Применение к нашему проекту

### Нужно ли переписывать всё? **Нет, но нужно реструктурировать.**

#### Что останется / переиспользуется:
- Логика чтения энкодеров и отправки команд на моторы → переедет в `read()` и `write()` Hardware Interface
- URDF (когда напишем) — дополнится тегом `<ros2_control>`
- Параметры робота (wheel_base, wheel_radius) — переедут в URDF/YAML

#### Что станет **не нужным** (заменит ros2_control):
- `cmd_vel_callback()` → этим займётся `diff_drive_controller`
- `actual_speeds()` и кинематика → `diff_drive_controller`
- `odometry_publish()` → `diff_drive_controller`
- `publish_tf()` → `diff_drive_controller`
- `timer_callback()` → цикл управления controller_manager

#### Что появится **нового**:
- `MyRobotHardware` (C++ класс) — `read()` / `write()` для Serial/CAN
- `controllers.yaml` — конфиг параметров контроллеров
- Дополнение к URDF с тегом `<ros2_control>`

### Схема "до" и "после":

```
ДО (сейчас):
/cmd_vel → motor_driver_node.py → [кинематика + одометрия + TF + железо]

ПОСЛЕ (ros2_control):
/cmd_vel → diff_drive_controller → [command_interfaces] → MyRobotHardware.write() → Железо
                                                          MyRobotHardware.read()  ← Железо
           diff_drive_controller ← [state_interfaces]  ← (автоматически)
           diff_drive_controller → /odom + TF (автоматически!)
```

---

## 7. Рекомендуемый план перехода

1. **Сейчас** — Закончить текущий `motor_driver_node.py` (шаги 1.5, 2.x).
   Это ценный опыт: ты понимаешь, что именно делает ros2_control за тебя.

2. **Шаг 2.1** — Написать URDF (обязательно, нужен для обоих подходов).

3. **Параллельно** — Изучить пример Hardware Interface:
   - `ros2_control_demos` (официальные примеры)
   - `diffdrive_arduino` (пример для Serial + дифференциального привода)

4. **Шаг Hardware Interface** (новый этап):
   - Создать пакет `guide_robot_hardware_interface` (C++)
   - Реализовать класс `GuideRobotHardware : SystemInterface`
   - Реализовать `read()` — чтение из Serial/CAN
   - Реализовать `write()` — отправка команд

5. **Финал** — Убрать `motor_driver_node.py`, запустить через controller_manager.

---

## 8. Важные ссылки

- Официальная документация: https://control.ros.org/
- Готовый пример с Serial (Arduino): https://github.com/joshnewans/diffdrive_arduino
- Пример ros2_control_demos: https://github.com/ros-controls/ros2_control_demos
- Туториал по написанию Hardware Interface:
  https://control.ros.org/master/doc/ros2_control/hardware_interface/doc/writing_new_hardware_interface.html
