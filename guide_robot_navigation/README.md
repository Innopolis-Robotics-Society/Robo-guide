# guide_robot_navigation

ROS 2 Humble пакет с launch-файлами и параметрами Nav2 / SLAM Toolbox для
робота-экскурсовода Guide-Robot. Пакет не содержит своих C++/Python нод — это
чисто конфигурационно-оркестрационный слой поверх стандартных серверов
`nav2_bringup`, `slam_toolbox` и `nav2_collision_monitor`.

## Обзор

Структура:

- `config/` — исходные конфиги.
  - `first_iter_nav2.yaml.in` — **шаблон** параметров всего стека Nav2
    (amcl, bt_navigator, controller_server, костмапы, planner_server,
    behavior_server, velocity_smoother, waypoint_follower,
    collision_monitor). Содержит плейсхолдеры `${section.key}`.
  - `mapper_params_online_async.yaml` — параметры `slam_toolbox`, обычный
    (не шаблонный) yaml.
- `generated_config/first_iter_nav2.yaml` — **результат рендера** шаблона
  выше (см. "Файлы запуска / генерация" ниже). Закоммичен в git.
- `guide_robot_navigation/` — python-модуль пакета; фактически пустой
  (`__init__.py` присутствует только чтобы `ament_python` собрал пакет),
  никакого рантайм-кода нет.
- `launch/` — три launch-файла (`common`, `navigation`, `slam_navigation`).
- `map/` — две пары `.yaml`/`.pgm`: `lab_map` (лаборатория) и `simple`.
- `test/` — единственный тест, `test_copyright.py`, и тот выключен.
- `resource/guide_robot_navigation` — пустой маркер ament-индекса.

Пакет подключается из `guide_robot_bringup` (`hardware.launch.py` для
реального робота, `simulation.launch.py` для Stage/Gazebo-подобного стенда);
сам по себе он не проверяет, что переданы корректные аргументы, и опирается
на то, что вызывающий launch всегда подставляет свои значения `map` и
`*_params_file` (см. "Известные проблемы", пп. 2–3).

### Механизм генерации `generated_config`

`setup.py:29-34` при каждой сборке пакета (`colcon build`, `pip install -e .`)
импортирует `guide_robot_description/scripts/render_params.py` и вызывает
`render()` для каждого файла `config/*.in`, подставляя значения из
`guide_robot_description/config/robot_params.yaml` (единый источник правды
по физическим параметрам робота — лимиты скорости/ускорения, геометрия
корпуса, диапазон лидара) и записывая результат в
`generated_config/<имя_без_.in>`. Незнакомый ключ в шаблоне — это ошибка
сборки (`render_params.py:47-51`), а не тихая подстановка `None`.

`setup.py:43` устанавливает в `share/guide_robot_navigation/config` **только**
файлы из `generated_config/*.yaml`. `mapper_params_online_async.yaml` в этот
глоб не попадает никаким образом — см. "Известные проблемы", п. 1, это
ломает SLAM в собранном workspace.

`generated_config/first_iter_nav2.yaml` при этом закоммичен в git как
обычный файл, хотя это полностью производный артефакт — риск расхождения
разобран в п. 4 ниже.

## Конфигурация Nav2

Все параметры ниже — из `generated_config/first_iter_nav2.yaml` (сгенерирован
из `config/first_iter_nav2.yaml.in`); номера строк даны по обоим файлам,
так как построчно они идентичны.

**Локализация (AMCL, `:1-67`)** — `likelihood_field`, `laser_max_range`
подставляется из `sensors.lidar_range_max` = 12.0 (реальная дальность
RPLIDAR C1, раньше стояло дефолтное 100.0, поправлено 2026-07-27, см.
комментарий `:17-22`). `set_initial_pose: true` с жёстко заданной позой
`(0, 0, 0)` (`:61-66`) — годится только пока робот физически стартует из
одной и той же точки; см. "Известные проблемы", п. 5.

**bt_navigator (`:76-141`)** — своего BT XML в пакете нет, используется
дефолтное дерево Humble
`navigate_to_pose_w_replanning_and_recovery.xml`
(старый параметр `default_bt_xml_filename` из Galactic был в файле, но
Humble его не объявляет и молча игнорировал — снят, задокументировано на
месте, `:82-100`). Именно в этом дереве зашит recovery `RoundRobin` со
`Spin 1.57 рад` → `Wait 5 c` → `BackUp`, который раньше срабатывал на
обычных разворотах (см. следующий пункт). Groot-мониторинг выключен
(`enable_groot_monitoring: False`, `:107`) — иначе ZMQ публикует состояние
дерева на 100 Гц независимо от подписчиков.

**controller_server / FollowPath** — голый `dwb_core::DWBLocalPlanner`.
С 27.07 по 30.07 он был обёрнут в
`nav2_rotation_shim_controller::RotationShimController`, чтобы робот
сначала доворачивался на месте, а не заходил в цель по дуге (у DWB нет
режима "сначала поворот, потом ход", дуга дешевле по критикам, но выносит
корпус в сектор, не просматриваемый лидаром на локальном rolling-window
костмапе, который не трекает unknown space). **Шим убран 30.07**: боковой
сектор закрыт сонарами через `collision_monitor`, а сам шим дрался с
критиком `RotateToGoal` за финальный доворот — робот доезжал до цели,
доворачивался, раскручивался обратно, проезжал вперёд и снова докручивал.
Разбор механизма — в комментарии над `plugin:` в
`config/first_iter_nav2.yaml.in`. Заход на цель теперь снова идёт дугой.
Скорости/ускорения (`:251-265`) подставляются из `robot_params.yaml`
(`limits.*`), с сознательным запасом относительно лимитера
`diff_drive_controller`. `xy_goal_tolerance` у `general_goal_checker`
(`:183`) и у `FollowPath` (`:291`) равны намеренно — комментарий `:278-290`
подробно объясняет, что рассогласование этих двух допусков и было
источником "приехал → дёргается → уходит в recovery". Критик
`RotateToGoal` (`:306-308`) — единственное, что заставляет робота
довернуться на месте в конце пути.

**Костмапы (local `:314-387`, global `:389-440`)** — `voxel_layer` убран из
обоих (планарный лидар, 3D-воксели избыточны), плагины: local —
`obstacle_layer + inflation_layer`, global — добавлен `static_layer`.
`robot_radius: 0.43` в обоих (цилиндр r=0.31 с центром base_link co
смещением x=-0.12, см. `guide_robot_description/config/robot_params.yaml` —
геометрически подтверждено, см. п. 6). `inflation_radius: 1.0` сведено
между слоями (была рассинхронизация 0.55 vs 1.0 — планировщик и локальный
костмап расходились в оценке одного и того же прохода, `:345-358`).
Локальное окно расширено до 5×5 м / 0.05 (`:331-333`) под `sim_time: 2.0`
DWB на `max_vel_x: 0.5`. `raytrace_max_range`/`obstacle_max_range`
пересчитаны под это окно (`:370-377`).

**planner_server (`:458-477`)** — `NavfnPlanner`, `use_astar: false`
(Дijkstra, не A*), `allow_unknown: true`. `expected_planner_frequency: 1.0`
подогнано под реальную частоту вызова из BT (`RateController hz="1.0"`),
иначе лог заливало предупреждениями "missed its desired rate" (`:460-465`).

**behavior_server (`:482-507`)** — переименован из `recoveries_server`
начиная с Galactic; комментарий `:479-481` явно отмечает, что старое имя
секции молча не читалось и сервер работал на дефолтных 1.0 рад/с вместо
физических 0.5236. `max_rotational_vel` теперь синхронизирован с базой.

**velocity_smoother (`:520-533`)** — последняя ступень перед колёсами
(`controller_server → cmd_vel_nav → velocity_smoother → cmd_vel_smoothed →
cmd_vel`). Лимиты — прямая подстановка `limits.*` из `robot_params.yaml`,
без запаса (в отличие от DWB, где запас есть, `:245-250`). Без этой секции
сервер поднимался бы на дефолтах Nav2 (`[0.5, 0, 2.5]` / accel `[2.5, 0,
3.2]`), в 3–5 раз шире всего остального стека.

**collision_monitor (`:548-626`)** — не входит в `nav2_bringup`, поднимается
отдельной нодой в `launch/common.launch.py:50-58` под отдельным
lifecycle-менеджером `lifecycle_manager_safety` (`:62-73`), чтобы аварийный
слой переживал рестарт основного нав-стека. Источники: `/scan` +
7 дальномеров `/sonar/range/sonar_sensor_{1,2,4,5,6,8,9}` (весь набор
сонаров, которые физически есть на роботе — id 3 и 7 не существуют).
Три полигона:
- `FootprintApproach` — динамический, по предсказанной траектории робота
  (`footprint_topic`, `time_before_collision: 1.5 с`) — единственный
  полигон, который реально учитывает текущую скорость.
- `SonarStopFront` — статичный прямоугольник 0.4×0.56 м спереди,
  `action_type: stop`.
- `SonarSlow` — статичный прямоугольник 0.65×0.68 м, `action_type:
  slowdown`, `slowdown_ratio: 0.4`.

`cmd_vel_out_topic` смотрит прямо в
`/diff_drive_controller/cmd_vel_unstamped` — collision_monitor стоит
последним звеном перед аппаратным драйвером, после `velocity_smoother`.

**SLAM (`config/mapper_params_online_async.yaml`)** — `slam_toolbox`,
`solver_plugin: CeresSolver`, `mode: mapping`, `scan_topic: /scan`,
`resolution: 0.05` (совпадает с картами и костмапами). Параметры не
шаблонизированы через `robot_params.yaml` — писались вручную и с картами
не завязаны.

**smoother_server** — в `first_iter_nav2.yaml.in` нет секции для него, но
`nav2_bringup/launch/navigation_launch.py` в Humble поднимает
`smoother_server` безусловно как lifecycle-ноду. Сейчас это безвредно:
сервер стартует на встроенных дефолтах (`simple_smoother`), а дефолтное
BT-дерево `Smooth`-экшен вообще не вызывает — но это единственный сервер
в стеке, чьи параметры не осмыслены явно, в отличие от всего остального
файла, где каждое значение прокомментировано.

## Карты

| Файл | Размер, px | resolution | origin (x, y, yaw) | Физический размер |
|---|---|---|---|---|
| `lab_map.pgm`/`.yaml` | 560×482 | 0.05 | (-6.04, -12.9, 0) | ≈28.0×24.1 м |
| `simple.pgm`/`.yaml` | 379×468 | 0.05 | (-10.8, -11.5, 0) | ≈19.0×23.4 м |

Обе — `mode: trinary`, `occupied_thresh: 0.65`, `free_thresh: 0.25`,
согласовано с `map_saver` в `first_iter_nav2.yaml.in:450-456`.

Проверена геометрическая состоятельность жёстко заданной позы AMCL
`(0,0,0)` (`generated_config/first_iter_nav2.yaml:52-60`): пиксель
`lab_map.pgm[row=223, col=120]` (пересчёт мировых координат `(0,0)` через
`origin`/`resolution`) имеет значение 254 (свободно), как и заявлено в
комментарии рядом с параметром. Это подтверждает только геометрическую
корректность точки на карте — не то, что робот физически стартует именно
оттуда (см. "Известные проблемы", п. 5).

`navigation.launch.py:31` использует как дефолт несуществующий
`map/map.yaml` — см. п. 3.

## Файлы запуска

- **`launch/common.launch.py`** — общее ядро: `nav2_bringup
  navigation_launch.py` (`use_composition: False`, чтобы respawn/remap были
  предсказуемы на реальном железе) + отдельная нода `collision_monitor` +
  отдельный `lifecycle_manager_safety` (`node_names: ["collision_monitor"]`,
  `bond_timeout: 4.0`). Аргументы: `use_sim_time`, `autostart_nav`,
  `nav2_params_file` (дефолт `config/first_iter_nav2.yaml` — резолвится
  через `get_package_share_directory`, т.е. в **установленный** share, а не
  в исходники, так что это именно рендеренный файл).
- **`launch/navigation.launch.py`** — добавляет `nav2_bringup
  localization_launch.py` (`map_server` + `amcl`) поверх `common`. Аргументы:
  `use_sim_time`, `autostart_nav`, `map` (дефолт `map/map.yaml` —
  **не существует**, см. п. 3), `nav2_params_file`.
- **`launch/slam_navigation.launch.py`** — вместо локализации по карте
  поднимает `slam_toolbox online_async_launch.py` + `common`. Аргумент
  `slam_params_file` по умолчанию указывает на
  `<share>/params/mapper_params_online_async.yaml` — каталога `params/` в
  пакете нет вообще, правильный — `config/` (см. п. 2).
- Оба верхнеуровневых launch-файла `guide_robot_bringup` (`hardware.launch.py`,
  `simulation.launch.py`) корректно переопределяют `map` и
  `slam_params_file` при инклюде (используют `config/` и реальные карты
  `lab_map.yaml`/`simple.yaml`), поэтому баги в дефолтах (пп. 2–3) не
  проявляются в штатном пути запуска через bringup — только при прямом
  `ros2 launch guide_robot_navigation ...` без обёртки.

## Известные проблемы и замечания

Порядок — по значимости для безопасности/корректности в реальной
эксплуатации рядом с людьми, затем упаковка, затем мелочи.

1. **AMCL стартует с жёстко заданной позой `(0, 0, 0)` и
   `set_initial_pose: true`** (`generated_config/first_iter_nav2.yaml:52-66`).
   Комментарий в самом файле честно предупреждает: это верно только если
   робот физически паркуется в одной и той же точке. Для тур-гида,
   которого могут переставить/докатить руками до другого места перед
   запуском смены, это означает уверенный, но неверный `map→odom` — стек
   будет действовать так, будто локализован правильно, двигаясь среди
   людей. Нужна либо процедурная гарантия (робот всегда стартует из
   одного дока), либо возврат на ручной "2D Pose Estimate" через RViz для
   публичных прогонов, либо детекция дока.
2. **Полигоны сонара в `collision_monitor` не масштабируются по скорости.**
   `SonarStopFront`/`SonarSlow` (`:572-589`) — статичные прямоугольники
   (0.35 м / 0.55 м вперёд по x), не зависящие от текущей скорости, при
   максимальной `max_linear_velocity: 0.6 м/с`
   (`guide_robot_description/config/robot_params.yaml`, отрендерено в
   `velocity_smoother.max_velocity`, `:526`). Только лидарный
   `FootprintApproach` (`time_before_collision: 1.5 с`) реально учитывает
   скорость. Перед эксплуатацией рядом с людьми стоит проверить
   тормозной путь с манекеном человеческого роста на максимальной
   скорости, а не полагаться только на геометрию полигонов на бумаге.
