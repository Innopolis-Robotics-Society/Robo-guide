# TF2 в ROS 2 — Полное объяснение

## 1. Что такое TF и зачем он нужен?

Представь: у тебя есть лидар, который видит препятствие в 50 см от себя.
Но навигации нужно знать, где это препятствие находится **относительно центра робота**.
А карте — относительно **начала координат мира**.

**TF (Transform)** — это система, которая знает, **как одна система координат расположена относительно другой**,
и умеет автоматически пересчитывать любые данные между фреймами.

Технически: TF хранит дерево **геометрических преобразований** (позиция + ориентация) между именованными **фреймами**.

---

## 2. Архитектура TF: как это работает внутри

### Фреймы (Frames)
Каждый **фрейм** — это система координат с именем (строка). Например:
- `map` — глобальная система координат (начало координат мира)
- `odom` — система координат одометрии (где робот стартовал)
- `base_link` — центр корпуса робота
- `lidar_link` — точка, где физически стоит лидар
- `camera_link` — точка, где стоит камера

### Дерево трансформаций
TF хранит не отдельные пары, а **дерево** (tree). Каждый фрейм — узел,
каждое преобразование — ребро (направленное: от родителя к ребёнку).

```
map
 └── odom
      └── base_link
           ├── lidar_link
           ├── camera_link
           └── wheel_left_link
           └── wheel_right_link
```

Зная каждое ребро, TF умеет вычислить **любой путь** в дереве.
Например: где лидар (`lidar_link`) относительно карты (`map`)?
TF сам посчитает: `map → odom → base_link → lidar_link`.

### Буфер и время
TF хранит **историю** трансформаций (по умолчанию ~10 секунд).
Это нужно, чтобы можно было спросить: "Где был робот 0.3 секунды назад?"
Это критично для синхронизации сенсоров с разными задержками.

### Топики в ROS 2
TF использует два топика:
| Топик | Тип | Назначение |
|---|---|---|
| `/tf` | `tf2_msgs/msg/TFMessage` | Динамические трансформы (меняются со временем: odom→base_link) |
| `/tf_static` | `tf2_msgs/msg/TFMessage` | Статические трансформы (неизменные: base_link→lidar_link) |

Все узлы, которым нужны трансформации, **подписываются** на эти топики
и складывают данные в свой локальный буфер.

---

## 3. Стандартное дерево фреймов для мобильного робота

```
map ──────────────────────────► odom ──────────────────────────► base_link
     (публикует: AMCL / Nav2)        (публикует: наш motor_driver_node)
     Тип: динамический               Тип: динамический
     Смысл: поправка локализации     Смысл: накопленная одометрия

base_link ──────────────────────► lidar_link
          (публикует: robot_state_publisher из URDF)
          Тип: статический
          Смысл: где лидар стоит на корпусе
```

### Кто публикует что?

| Трансформ | Кто публикует | Когда |
|---|---|---|
| `map → odom` | AMCL (пакет локализации) | Когда работает Nav2 |
| `odom → base_link` | **Наш `motor_driver_node`** | Каждые ~50ms (таймер) |
| `base_link → lidar_link` | `robot_state_publisher` | Из URDF (статически) |
| `base_link → camera_link` | `robot_state_publisher` | Из URDF (статически) |

**Важно:** каждая стрелка — это **ровно один** публикующий узел.
Если два узла попытаются публиковать один и тот же трансформ — будет конфликт.

---

## 4. Синтаксис Python: TransformBroadcaster

### 4.1 Публикация динамического трансформа (наш случай)

```python
from tf2_ros import TransformBroadcaster
from geometry_msgs.msg import TransformStamped

class MyNode(Node):
    def __init__(self):
        super().__init__('my_node')
        # Создаём broadcaster — он публикует в /tf
        self.tf_broadcaster = TransformBroadcaster(self)

    def publish_transform(self):
        t = TransformStamped()

        # --- Заголовок (ОБЯЗАТЕЛЬНО) ---
        t.header.stamp = self.get_clock().now().to_msg()  # текущее время
        t.header.frame_id = 'odom'       # РОДИТЕЛЬ (от кого)
        t.child_frame_id = 'base_link'   # РЕБЁНОК (к кому)

        # --- Позиция (translation) ---
        t.transform.translation.x = 1.5   # метры
        t.transform.translation.y = 0.3
        t.transform.translation.z = 0.0   # для плоского робота всегда 0

        # --- Ориентация (rotation) в кватернионах ---
        # Кватернион для поворота только вокруг оси Z (рыскание/yaw):
        # q = (0, 0, sin(theta/2), cos(theta/2))
        import math
        theta = 0.785  # 45 градусов в радианах
        t.transform.rotation.x = 0.0
        t.transform.rotation.y = 0.0
        t.transform.rotation.z = math.sin(theta / 2.0)
        t.transform.rotation.w = math.cos(theta / 2.0)

        # --- Публикация ---
        self.tf_broadcaster.sendTransform(t)
```

### 4.2 Публикация статического трансформа (для сенсоров)

```python
from tf2_ros import StaticTransformBroadcaster

class MyNode(Node):
    def __init__(self):
        super().__init__('my_node')
        # StaticTransformBroadcaster — публикует в /tf_static
        self.static_broadcaster = StaticTransformBroadcaster(self)

        # Публикуем один раз в __init__ — дальше он не меняется
        self._publish_lidar_tf()

    def _publish_lidar_tf(self):
        t = TransformStamped()
        t.header.stamp = self.get_clock().now().to_msg()
        t.header.frame_id = 'base_link'   # родитель
        t.child_frame_id = 'lidar_link'   # ребёнок

        # Лидар стоит на 20 см спереди и 30 см выше центра
        t.transform.translation.x = 0.2
        t.transform.translation.y = 0.0
        t.transform.translation.z = 0.3

        # Лидар не повёрнут относительно корпуса
        t.transform.rotation.x = 0.0
        t.transform.rotation.y = 0.0
        t.transform.rotation.z = 0.0
        t.transform.rotation.w = 1.0  # единичный кватернион = нет поворота

        self.static_broadcaster.sendTransform(t)
```

### 4.3 Чтение трансформа (подписка / lookup)

```python
from tf2_ros import Buffer, TransformListener

class MyNode(Node):
    def __init__(self):
        super().__init__('my_node')
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

    def get_robot_position_in_map(self):
        try:
            # Где находится base_link относительно map прямо сейчас?
            transform = self.tf_buffer.lookup_transform(
                'map',        # целевой фрейм (куда пересчитываем)
                'base_link',  # исходный фрейм (что ищем)
                rclpy.time.Time()  # Time() = последний доступный
            )
            x = transform.transform.translation.x
            y = transform.transform.translation.y
            self.get_logger().info(f"Робот в карте: x={x:.2f}, y={y:.2f}")
        except Exception as e:
            self.get_logger().warn(f"TF недоступен: {e}")
```

---

## 5. Кватернионы — коротко и понятно

Кватернион `(x, y, z, w)` — способ описать поворот в 3D без проблемы "gimbal lock".

Для нашего **плоского робота** поворот только вокруг вертикальной оси Z (yaw/рыскание):

```
x = 0
y = 0
z = sin(theta / 2)   # theta — угол в радианах
w = cos(theta / 2)
```

| Угол (degrees) | theta (rad) | z | w |
|---|---|---|---|
| 0° | 0.0 | 0.0 | 1.0 |
| 90° | π/2 ≈ 1.571 | 0.707 | 0.707 |
| 180° | π ≈ 3.142 | 1.0 | 0.0 |
| -90° | -π/2 | -0.707 | 0.707 |

**Единичный кватернион** `(0, 0, 0, 1)` = никакого поворота нет.

---

## 6. Типичные ошибки

| Ошибка | Причина | Решение |
|---|---|---|
| `"waiting for transform"` | Трансформ ещё не пришёл | Подождать, или проверить что broadcaster работает |
| `"extrapolation into the future"` | timestamp сообщения новее последнего TF | Использовать одинаковый `stamp` в TF и данных |
| `"could not find a connection"` | Фреймы не связаны в дереве | Проверить, что все звенья цепи публикуются |
| Два узла публикуют один трансформ | Конфликт | Один из узлов должен отключить публикацию TF |

---

## 7. Инструменты отладки

```bash
# Посмотреть всё дерево TF прямо сейчас
ros2 run tf2_tools view_frames

# Вывести конкретный трансформ в терминал
ros2 run tf2_ros tf2_echo odom base_link

# Проверить, что публикуется в /tf
ros2 topic echo /tf

# Список всех известных фреймов
ros2 run tf2_ros tf2_monitor
```

---

## 8. Итог: наш motor_driver_node

В нашем случае:
- `odom` — начало координат (там, где робот стартовал, фиксированная точка)
- `base_link` — центр робота (двигается вместе с роботом)
- **Мы публикуем** трансформ `odom → base_link` в каждом вызове `odometry_publish()`
- Данные для трансформа берём из интегрированной одометрии: `self.x`, `self.y`, `self.theta`
- Позиция в TF **всегда должна совпадать** с позицией в сообщении `/odom`
