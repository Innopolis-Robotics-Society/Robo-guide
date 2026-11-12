# C++ ROS2 Hardware Interface — объяснение с нуля

## Большая картина: что мы строим

```
controller_manager (ros2_control_node)
        │
        │  хочет управлять колёсами робота
        │  но не знает как общаться с железом
        │
        ▼
  [загружает плагин]
        │
        ▼
  GuideRobotSystem  ← это наш C++ класс
        │
        │  знает как открыть serial порт
        │  знает формат Dynamixel пакета
        │
        ▼
  /dev/ttyUSB0 → моторы
```

**Плагин** — это `.so` файл (shared object, аналог .dll в Windows).
`controller_manager` загружает его в runtime через pluginlib, не зная заранее
ничего о нашем коде.

---

## Зачем нужен каждый файл

### `guide_robot_hardware.xml` — паспорт плагина

```xml
<library path="guide_robot_hardware">
  <class name="guide_robot_hardware/GuideRobotSystem"
         type="guide_robot_hardware::GuideRobotSystem"
         base_class_type="hardware_interface::SystemInterface">
```

Это **единственная связь** между:
- Строкой в URDF: `<plugin>guide_robot_hardware/GuideRobotSystem</plugin>`
- Реальным C++ классом в `.so` файле

Без него pluginlib не знает какой класс грузить.
Аналогия: это как запись в телефонной книге — "по имени A найди номер B".

```
URDF xacro
  <plugin>guide_robot_hardware/GuideRobotSystem</plugin>
          │
          │ pluginlib ищет в xml-файле
          ▼
  guide_robot_hardware.xml
    name="guide_robot_hardware/GuideRobotSystem"  ← совпадает!
    type="guide_robot_hardware::GuideRobotSystem" ← вот реальный C++ класс
          │
          │ pluginlib загружает .so и создаёт объект
          ▼
  libguide_robot_hardware.so
    class GuideRobotSystem { ... }
```

---

### `guide_robot_system.hpp` — заголовочный файл (объявление)

`.hpp` файл отвечает на вопрос **"ЧТО умеет класс?"**.

Это контракт: "у меня есть такие методы и такие поля".
Сам код методов здесь НЕ написан — только их сигнатуры.

```cpp
class GuideRobotSystem : public hardware_interface::SystemInterface
{
public:
  CallbackReturn on_init(...);    // ОБЪЯВЛЕНИЕ — что принимает и возвращает
  CallbackReturn on_configure(..);
  return_type write(...);
  // ...

private:
  int serial_fd_;          // поля класса
  double left_vel_cmd_;
};
```

Зачем разделять объявление и реализацию?
- Другие файлы могут `#include` заголовок и знать интерфейс класса,
  не компилируя весь код заново
- В больших проектах ускоряет компиляцию
- Чётко видно "публичный контракт" класса

---

### `guide_robot_system.cpp` — реализация (КАК делает)

`.cpp` файл отвечает на вопрос **"КАК работает класс?"**.

Здесь написан реальный код каждого метода:

```cpp
// Включаем заголовок чтобы знать сигнатуры
#include "guide_robot_hardware/guide_robot_system.hpp"

// Реализация on_configure:
CallbackReturn GuideRobotSystem::on_configure(...)
{
  // РЕАЛЬНЫЙ код открытия порта
  serial_fd_ = open("/dev/ttyUSB0", O_RDWR);
  // ...
}

// Реализация write:
return_type GuideRobotSystem::write(...)
{
  // РЕАЛЬНЫЙ код формирования Dynamixel пакета и отправки
  uint8_t packet[] = {0xFF, 0xFF, 0xFE, ...};
  ::write(serial_fd_, packet, sizeof(packet));
}
```

---

## Зачем нужны папки `include/` и `src/`

Это стандартная конвенция C++ (и ROS2 в частности):

```
guide_robot_hardware/
│
├── include/                    ← ПУБЛИЧНЫЕ заголовки
│   └── guide_robot_hardware/   ← папка = имя пакета (обязательно!)
│       └── guide_robot_system.hpp
│
├── src/                        ← ПРИВАТНАЯ реализация
│   └── guide_robot_system.cpp
│
├── guide_robot_hardware.xml    ← паспорт плагина (в корне пакета)
├── CMakeLists.txt
└── package.xml
```

**Почему `include/guide_robot_hardware/`?**

В CMakeLists.txt написано:
```cmake
target_include_directories(${PROJECT_NAME} PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
```
Компилятор видит папку `include/` как корень для поиска заголовков.
Поэтому чтобы написать:
```cpp
#include "guide_robot_hardware/guide_robot_system.hpp"
```
файл должен лежать именно по пути `include/guide_robot_hardware/guide_robot_system.hpp`.

Это также позволяет избежать конфликтов имён между пакетами:
- `guide_robot_hardware/guide_robot_system.hpp`  ← наш
- `other_package/guide_robot_system.hpp`          ← чужой, не конфликтует

---

## Жизненный цикл плагина (порядок вызова методов)

```
ros2 launch ... use_mock_hardware:=false
        │
        ▼
controller_manager читает URDF → находит <plugin>guide_robot_hardware/GuideRobotSystem</plugin>
        │
        ▼
pluginlib загружает .so файл, создаёт объект GuideRobotSystem
        │
        ▼
on_init(info)           ← читаем параметры из URDF <param> тегов
        │                  (serial_port, baud_rate, left_wheel_id...)
        ▼
on_configure()          ← открываем serial порт, проверяем соединение
        │
        ▼
export_state_interfaces()    ← "у меня есть position и velocity двух колёс"
export_command_interfaces()  ← "я принимаю velocity команды для двух колёс"
        │
        ▼
on_activate()           ← робот готов принимать команды
        │
        ▼
  ┌─────────────────────────────────┐
  │  ЦИКЛ 50 Гц (update_rate: 50)  │
  │                                 │
  │  read()  ← обновить state       │
  │  write() ← отправить команду    │
  └─────────────────────────────────┘
        │
        ▼  (при Ctrl+C или ros2 lifecycle деактивации)
on_deactivate()         ← послать стоп, закрыть порт
```

---

## Как связаны все файлы

```
package.xml
  └── объявляет зависимости: hardware_interface, pluginlib, rclcpp

CMakeLists.txt
  ├── find_package(hardware_interface) → базовый класс SystemInterface
  ├── find_package(pluginlib)          → макрос PLUGINLIB_EXPORT_CLASS
  ├── add_library(... SHARED ...)      → компилирует .cpp → .so
  └── pluginlib_export_plugin_description_file(... .xml)
          └── устанавливает .xml в share/ и регистрирует в ament index

guide_robot_hardware.xml
  └── говорит pluginlib: "в .so есть класс GuideRobotSystem"

guide_robot_system.hpp
  └── объявляет класс GuideRobotSystem (что умеет)

guide_robot_system.cpp
  ├── реализует методы класса (как умеет)
  └── PLUGINLIB_EXPORT_CLASS(...)
          └── встраивает в .so специальный символ,
              который pluginlib ищет при загрузке
```

---

## Итого: минимальный набор для работающего плагина

| Файл | Роль | Без него |
|---|---|---|
| `guide_robot_hardware.xml` | Паспорт плагина | pluginlib не найдёт класс |
| `guide_robot_system.hpp` | Объявление класса | `.cpp` не скомпилируется |
| `guide_robot_system.cpp` | Реализация + PLUGINLIB_EXPORT_CLASS | Нет `.so`, нечего грузить |
| `CMakeLists.txt` | Сборка `.so` и установка | Пакет не соберётся |
| `package.xml` | Зависимости | colcon не найдёт нужные пакеты |
