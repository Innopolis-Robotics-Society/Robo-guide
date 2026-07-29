# guide_robot_simulation

## Обзор

Пакет `ament_python`, отвечающий за запуск робота Guide-Robot в **Gazebo Classic 11**
(через `gazebo_ros`), а не в Stage и не в Gazebo Ignition/Fortress. Никаких
следов альтернативных симуляторов в пакете нет — выбор однозначный.

Состав:
- `launch/gazebo.launch.py` — поднимает Gazebo, спавнит робота, запускает
  контроллеры и вспомогательные узлы;
- `guide_robot_simulation/sonar_merge.py` — Python-нода, агрегирующая
  сонары в одно сообщение `guide_robot_msgs/SonarRanges`;
- `worlds/simple.world` — тестовый мир SDF 1.7 с 12 статичными коробками-
  препятствиями;
- `resource/`, `test/test_copyright.py`, `package.xml`, `setup.py`,
  `setup.cfg` — стандартный минимум для `ament_python`.

Пакет **активно используется и поддерживается**, а не является заброшенной
альтернативой: он подключается из
`guide_robot_bringup/launch/simulation.launch.py` (единственная реальная
точка входа для полного симуляционного стека — Gazebo + локализация + Nav2 +
supervisor + RViz), и последние правки в этой части дерева датированы
2026-07-24…27, т.е. буквально последними днями перед этим обзором
(`git log`: `5b42c31 feat: add fully working sim and sonars`,
`410d56c fix: del wrong group name`, `3cbde09 feat: add collision monitor`).
Заявление в CLAUDE.md о том, что "3D Gazebo simulation is not yet stable, use
Stage instead" относится к **другому**, не связанному репозиторию
(`iros_llm_swarm_*`) — здесь оно не подтверждается: Stage вообще не
фигурирует в этом пакете, а Gazebo — единственный и рабочий путь.

## Симулятор и миры

- Формат мира — Gazebo Classic SDF `1.7` (`worlds/simple.world`).
- Мир **намеренно не содержит модель робота** — она спавнится отдельно
  из топика `/robot_description` через `spawn_entity.py`, чтобы не получить
  двух роботов в сцене (см. комментарий в `worlds/simple.world:1-14`).
- Мир не содержит блок `<state>` — типичная ошибка "запёкшихся" координат
  из GUI-снапшота осознанно избегается (тот же комментарий).
- В мире: `sun` (direction light), `ground_plane`, физика `ode`
  (`max_step_size=0.001`, `real_time_update_rate=1000`), 12 статичных
  боксов `box_01`…`box_12` 1×1×1 м, расставленных вручную для тестирования
  SLAM/Nav2, и предустановленная камера `user_camera` для GUI.
- Модель робота берётся не из `guide_robot_simulation`, а генерируется на
  лету из `guide_robot_description` (`guide_robot.urdf.xacro`) с аргументом
  `use_sim:=true`, который подключает `guide_robot.gazebo.xacro`
  (`gazebo_ros2_control`, два лидара `laser_frame_left/right` → `/scan_left`,
  `/scan_right`, IMU → `/imu/data`, 7 сонаров → `/sonar/range/sonar_sensor_<id>`).
  Т.е. спавнится актуальная, не устаревшая копия модели — общий источник
  правды один (`guide_robot_description`), собственной геометрии робота
  в `guide_robot_simulation` нет.

## Файлы запуска

### `launch/gazebo.launch.py`

Единственный launch-файл пакета. Заголовочный докстринг (`gazebo.launch.py:1-18`)
называет файл `simulation.launch.py` и советует `ros2 launch
guide_robot_simulation simulation.launch.py` — **это устаревший/неверный
комментарий**, файл называется `gazebo.launch.py`, команда с таким именем
не сработает.

Порядок запуска (комментарий в шапке объясняет "почему" — известный рабочий
паттерн `gazebo_ros2_control`):
1. `robot_state_publisher` — публикует `robot_description`, полученный
   через `subprocess.run(["xacro", ...])` с последующим удалением XML-
   комментариев регуляркой (`re.sub(r"<!--.*?-->", ...)`, строка 85).
   Вызов `xacro` не обёрнут в try/except: при ошибке (например, если
   `guide_robot_description` не собран или xacro упал) полетит сырой
   `subprocess.CalledProcessError` без понятной диагностики для
   пользователя launch-файла.
2. `spawn_entity.py` — спавнит сущность `guide_robot` из топика
   `robot_description`, `z=0.15`.
3. `gazebo_ros/gazebo.launch.py` — запускается **последним** намеренно,
   чтобы `gazebo_ros2_control` успел получить `robot_description` от
   `robot_state_publisher` к моменту загрузки плагина.
4. `joint_state_broadcaster` — с задержкой `TimerAction(period=2.0)`
   (жёсткая задержка, не событие) вместо ожидания реальной готовности
   Gazebo/спавна — потенциально хрупко на медленных машинах/CI.
5. `diff_drive_controller` — запускается по `OnProcessExit` после
   `joint_state_broadcaster_spawner`, что надёжнее пункта 4.
6. `rqt_robot_steering` — GUI-джойстик для ручного управления, publish
   на `/cmd_vel` без ремаппинга по умолчанию.

Устанавливает `GAZEBO_MODEL_PATH` (родитель `guide_robot_description` +
папка `meshes`) и обнуляет `GAZEBO_MODEL_DATABASE_URI`, чтобы Gazebo не
пытался лезть в интернет за моделями в контейнере без сети — разумное и
явно прокомментированное решение (строки 65-72).

Два узла **закомментированы прямо в списке `LaunchDescription`** (строки
196, 198) и потому никогда не стартуют через этот launch-файл:
- `cmd_vel_relay` (`/cmd_vel` → `/diff_drive_controller/cmd_vel_unstamped`) —
  ремаппинг нужно делать вручную через `teleop_twist_keyboard -r ...`, как
  и написано в докстринге; отключение согласовано с документацией.
- `sonar_merge` — узел агрегации сонаров, описанный ниже, полностью
  выключен из графа запуска.

`guide_robot_bringup/launch/simulation.launch.py` подключает этот файл
через `IncludeLaunchDescription` с `use_sim_time:=true` и добавляет поверх
`dual_laser_merger` (`/scan_left`+`/scan_right`→`/scan`), SLAM/AMCL, Nav2,
supervisor и RViz — то есть `guide_robot_simulation` отвечает только за
"голую" симуляцию (Gazebo + робот + контроллеры), а весь стек навигации
собирается снаружи. Комментарий в `simulation.launch.py:5` утверждает, что
`gazebo.launch.py` включает ещё и RViz ("Gazebo + robot + RViz") — это тоже
неверно: RViz здесь не запускается, он поднимается отдельным узлом в самом
`simulation.launch.py` (строки 182-190).

### `guide_robot_simulation/sonar_merge.py`

Нода `sonar_aggregator` (внутреннее имя ROS-ноды не совпадает с именем
модуля/entry point `sonar_merge` — небольшая путаница в неймспейсе).
Подписывается на `sensor_msgs/Range` по маске
`"{input_prefix}/{sensor_id}"`, где `input_prefix` по умолчанию —
`"sonar/raw"`, а `sensor_id` — `"sonar_1"`, `"sonar_2"` и т.д. Итог:
ожидаемые топики — `sonar/raw/sonar_1`, `sonar/raw/sonar_2`, ….

**Это не совпадает с тем, что реально публикует Gazebo-плагин сонаров.**
`guide_robot_description/urdf/guide_robot.gazebo.xacro:192-200` ремапит
вывод каждого сонара на `sonar/range/sonar_sensor_<id>` (например,
`/sonar/range/sonar_sensor_1`), т.е. другой префикс (`sonar/range` вместо
`sonar/raw`) и другое имя сенсора (`sonar_sensor_1` вместо `sonar_1`).
При запуске `sonar_merge` в симуляции он не получит ни одного сообщения и
будет вечно публиковать пустой `SonarRanges`. Это, по всей видимости, и
есть причина, по которой узел закомментирован в `gazebo.launch.py:198` —
разработчики, похоже, знают о нерабочем состоянии и отключили его, но не
удалили и не поправили.

Дополнительно: результат агрегации (топик `sonars`,
`guide_robot_msgs/SonarRanges`) нигде в репозитории не потребляется —
ни Nav2 (там сонары читаются напрямую по отдельным топикам
`/sonar/range/sonar_sensor_<id>`, см.
`guide_robot_navigation/config/first_iter_nav2.yaml.in:591-625`), ни
supervisor, ни что-либо ещё. На аппаратном пути (`guide_robot_sonar`)
похожая агрегация тоже публикуется, но в другой топик (`sonar/ranges`, не
`sonars`) и с другим префиксом подписки. Итого `sonar_merge.py` в текущем
виде — мёртвый/нерабочий код: ни топики на входе не совпадают с
Gazebo-плагином, ни выход никем не читается.
