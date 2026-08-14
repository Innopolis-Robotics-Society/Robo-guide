# guide_robot_msgs

Пакет общих ROS 2 интерфейсов (`msg/`, `srv/`) для стека Guide-Robot. Согласно
`package.xml` входит в группу `rosidl_interface_packages` и предназначен
быть единственным местом, где остальные пакеты `guide_robot_*` определяют
общие типы сообщений/сервисов, не привязанные к конкретному узлу.

## Обзор

Фактически на данный момент пакет содержит **одно** сообщение
(`SonarRanges.msg`) и **ни одного** сервиса — каталог `srv/` пуст и содержит
только `.gitkeep`. Ни один другой пакет репозитория (`guide_robot_bringup`,
`guide_robot_description`, `guide_robot_hardware`, `guide_robot_navigation`,
`guide_robot_simulation`, `guide_robot_sonar`, `guide_robot_supervisor`) не
определяет собственных `msg/srv/action` — то есть `guide_robot_msgs`
задуман как центральный пакет интерфейсов, но фактически используется
почти не по назначению: для межпроцессного обмена состояниями и
диагностикой (например, супервизор в `guide_robot_supervisor/supervisor_node.py`)
применяются стандартные `diagnostic_msgs/DiagnosticArray`, `std_msgs/String`
и т.п., а не типы из этого пакета.

Зависимости, объявленные в `package.xml`: `std_msgs`, `sensor_msgs`
(оба — как `<depend>`, то есть автоматически build+exec+test). Это
корректно для формата `package_format3` и message-only пакета.

`CMakeLists.txt` вызывает `find_package()` для `std_msgs` и `sensor_msgs` и
передаёт их в `DEPENDENCIES` у `rosidl_generate_interfaces()` — это
необходимо и корректно, так как `SonarRanges.msg` напрямую использует
`std_msgs/Header` и (транзитивно, через `sensor_msgs/Range[]`) типы из
`sensor_msgs`. Проблем с генерацией rosidl не обнаружено.

## Сообщения и сервисы

### `SonarRanges.msg`

```
std_msgs/Header header
sensor_msgs/Range[] ranges
```

Назначение: агрегированный «снимок» показаний всех дальномеров (сонаров)
робота в одном сообщении — по одному `sensor_msgs/Range` на сенсор,
идентификация сенсора выполняется через `ranges[i].header.frame_id`
(например, `sonar_sensor_1`…`sonar_sensor_9`), а не через отдельное поле
id/индекс в самом `SonarRanges`.

Где реально используется (найдено grep'ом по всему репозиторию):

- **Больше не публикуется** из `guide_robot_sonar`: агрегирующий
  `sonar_node.py` удалён. Боевой `sonar_node_mult.py` (через
  `perception.launch.py`) публикует по одному `sensor_msgs/Range` на
  `sonar/range/<frame_id>` и **не использует `SonarRanges` вообще**.
- **Публикуется** в `guide_robot_simulation/guide_robot_simulation/sonar_merge.py`
  (`SonarAggregator`, подписывается на индивидуальные `sensor_msgs/Range` и
  собирает их в один `SonarRanges` на топик `sonars`). Узел
  зарегистрирован как `sonar_merge` в `setup.py` и добавлен в
  `guide_robot_simulation/launch/gazebo.launch.py`, **но строка
  `#sonar_merge,` в итоговом `LaunchDescription` закомментирована** — узел
  фактически не запускается.
- **Подписчиков не найдено нигде в репозитории.** Ни один узел не
  подписывается на `sonar/ranges`, `sonars` или на тип `SonarRanges`
  вообще (проверено через `grep -r "SonarRanges"` и
  `grep -r create_subscription` по всем пакетам).

Реальные потребители данных сонаров в проекте — nav2 Collision Monitor
(`guide_robot_navigation/generated_config/first_iter_nav2.yaml`,
источники `sonar_1…sonar_9` на топиках `/sonar/range/sonar_sensor_N`) и
watchdog супервизора (`guide_robot_supervisor/config/supervisor.yaml`,
`sonar_rate` следит за теми же топиками `/sonar/range/sonar_sensor_N`) —
оба используют **индивидуальные** `sensor_msgs/Range`, публикуемые
`sonar_node_mult.py`, а не агрегированный `SonarRanges`.

## Известные проблемы и замечания

- **Мёртвый интерфейс.** Единственный оставшийся потенциальный публикатор —
  `sonar_merge` в симуляции, и он явно отключён (закомментирован). Реальный
  боевой путь (`perception.launch.py` → `sonar_node_mult.py` →
  Collision Monitor / супервизор) этот тип не использует. Стоит либо
  удалить `SonarRanges` и `sonar_merge`, либо осознанно включить агрегацию
  там, где она нужна (единый топик для RViz/Foxglove вместо N отдельных
  `Range`).
- **Отсутствие инвариантов в определении сообщения.** `SonarRanges.msg`
  не документирован ни одним комментарием: неясно из самого файла,
  ожидается ли фиксированный порядок/количество элементов в `ranges`,
  обязательны ли все сенсоры или допустимы пропуски (в
  `sonar_merge.py` пропуски возможны — устаревшие показания
  отбрасываются по `timeout`, значит длина массива не гарантирована и
  идентификация сенсора **обязана** идти через вложенный
  `header.frame_id`). Это стоит явно закомментировать в `.msg`, иначе
  любой новый подписчик будет вынужден читать исходники узлов-издателей,
  чтобы понять контракт.
- **Дублирование заголовков.** У `SonarRanges` есть собственный
  `header.frame_id` (в обоих издателях жёстко `"base_link"`), при этом
  каждый вложенный `sensor_msgs/Range` уже несёт свой `header` с
  индивидуальным `frame_id` сенсора и собственным `stamp`. Внешний
  `header.stamp` используется как время «снимка», а внутренние `stamp`
  могут быть старше (кэш из `sonar_merge.py`) — это семантически ок, но
  нигде не задокументировано, что означает рассогласование штампов.
- **`srv/` — пустой каталог-заглушка.** Присутствует только `.gitkeep`,
  сервисов не определено. Если сервисы не планируются в обозримом
  будущем, каталог можно убрать; если планируются — стоит завести хотя бы
  один реальный `.srv`, чтобы не расходились ожидания по структуре
  пакета.
- **Пакет недоиспользуется как «общий» интерфейсный пакет.** Ни один
  другой `guide_robot_*` пакет не определяет здесь свои типы (супервизор,
  формейшены статусов и т.п. используют `diagnostic_msgs`/`std_msgs`
  напрямую). Название и место пакета в архитектуре («shared ROS 2
  message/service definitions») предполагают более широкое использование,
  чем один сонарный тип с нулевым числом активных подписчиков.
- **Мелкое замечание по `package.xml`.** `maintainer email="fabian@todo.todo"`
  — плейсхолдер, не заменённый на реальный адрес.
- Типы полей и единицы измерения проблем не вызывают: `SonarRanges`
  целиком опирается на уже готовые `std_msgs/Header` и
  `sensor_msgs/Range` (float32 `range`/`min_range`/`max_range`/
  `field_of_view`, `uint8 radiation_type` с константами `ULTRASOUND`/
  `INFRARED`), поэтому типичных проблем вроде смешения `float32`/`float64`
  или "голого" int вместо enum-констант в самом пакете нет — они
  унаследованы от стандартных сообщений и не переопределяются здесь.
