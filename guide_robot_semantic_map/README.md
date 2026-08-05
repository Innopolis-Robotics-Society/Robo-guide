# guide_robot_semantic_map

Read-only заземление робота-гида: единственный источник истины по «где
что» и «что про это можно сказать». Три `rclpy.lifecycle.LifecycleNode` +
клиент к штатному `nav2_route`. Пакет ничего не пишет, ничего не
генерирует, не знает ни про FSM тура, ни про LLM — только отдаёт то, что
уже лежит в `config/`/`content/`, и считает маршруты через Route Server.

`ament_python`, ROS 2 Humble. Практический справочник по факту реализации

## Топология

```
                     ┌─────────────────┐
   config/*.yaml ──► │ content_server  │──/system_event (языковой фолбэк)
                     └─────────────────┘
                      ~/get_exhibit_content

                     ┌─────────────────┐         TF map->base_link
   config/*.yaml ──► │ location_server │◄─────────────────────────┐
                     └─────────────────┘                          │
                ~/list_locations, ~/resolve_location,             │
                ~/list_tours                                      │
                                                                  │
                     ┌─────────────────┐                          │
   config/*.yaml ──► │  route_planner  │◄─────────────────────────┘
                     └────────┬────────┘
                      ~/estimate_route
                               │ action ComputeRoute
                               ▼
                     ┌─────────────────┐
graph.geojson ─────► │   route_server  │  чужой пакет (nav2_route)
                     └─────────────────┘

Вызывающая сторона (mission/LLM-слой)
```

Каждая нода читает свои данные независимо на `on_configure`; между собой
ноды не разговаривают напрямую (кроме `route_planner` → `route_server`).
Разделение данных умышленное (design §0.5): `graph.geojson` — топология
и проходимость для Route Server, `locations.yaml`/`tours.yaml` — позы,
зоны, категории, алиасы, туры.

## Ноды

### `content_server`

Загружает `content/*.yaml` целиком в память на `on_configure` — никакого
чтения с диска в рантайме. Ни одной кодовой ветки, порождающей текст:
нет файла для `exhibit_id`/языка — пустой `chunks[]` и `version=""`,
решение (переспросить, промолчать) принимает вызывающая сторона.

**Сервис**: `~/get_exhibit_content` (`GetExhibitContent`) —
`exhibit_id, mode, language` → `chunks[], version`.

Фолбэк языка: запрошенный → `default_language` → пусто. Молчаливая
подмена запрещена — факт фолбэка идёт `WARN`-логом и `SystemEvent`
(`semantic_map.content_language_fallback`).

**Параметры**: `content_dir` (пусто → `share/guide_robot_semantic_map/content`),
`default_language="ru"`.

### `location_server`

Читает `locations.yaml` + `tours.yaml` (данные) и `graph.geojson`
(только для валидации ссылок `graph_node` — граф как таковой ноде не
нужен). Битая ссылка на несуществующий узел графа = отказ активации.

**Сервисы**:
- `~/list_locations` (`ListLocations`) — `zone, category, near_only` →
  `Location[]`. `is_public=false` не отдаётся никогда, кроме запроса
  ровно за служебной категорией (`charging`/`service`), совпадающей с
  категорией самой локации.
- `~/resolve_location` (`ResolveLocation`) — fuzzy-резолв алиасов
  (`lib/matching.py`: точное совпадение → префикс словоформы → difflib);
  только среди публичных локаций. `confident` — top-score ≥ порога и
  отрыв от второго места ≥ margin.
- `~/list_tours` (`ListTours`) — `language` → `Tour[]` с уже выбранным
  языком названия тура.

`near_only=true` сортирует по евклидову расстоянию от TF `map →
base_link`, отсекает по `near_radius_m`. Если TF недоступен — `WARN` +
`SystemEvent`, но список **не** обнуляется: отдаётся без фильтра по
расстоянию (локации валидны независимо от TF, посчитать расстояние не
вышло — это не повод скрыть данные).

**Параметры**: `locations_file`, `tours_file`, `graph_file` (все пустые
→ `share/.../config/*`), `default_language="ru"`, `near_radius_m=5.0`.

### `route_planner`

Единственный клиент `nav2_msgs/action/ComputeRoute`. Направленная
pairwise матрица `route_cost`/`distance_m` между **всеми** локациями из
`locations.yaml` считается один раз на `on_activate` и держится в
памяти (design §0.7 — не диск, не фон, не допущение симметрии); при
7 локациях — 42 пары, ~150–200 мс. Единственное, что нельзя закэшировать
— лега «текущая поза робота → кандидат на первую остановку»: она живая,
на каждый вызов `~/estimate_route`, для каждого запрошенного id.

**Сервис**: `~/estimate_route` (`EstimateRoute`) — `ids[], optimize` →
`ordered_ids[], distance_m, duration_min, feasible`.

- `optimize=true` → Held-Karp (N≤12, точно) или nearest-neighbour +
  2-opt + or-opt (N>12, бюджет `tsp_time_budget_ms`) поверх `route_cost`
  как целевой функции (`lib/tsp.py`).
- `optimize=false` → порядок как пришёл, только оценка.
- `distance_m` — отдельная величина от `route_cost`: сумма евклидовых
  отрезков `nav_msgs/Path`, а не скор TSP. Совпадают только при
  единственном `DistanceScorer` c `weight=1.0`.
- `duration_min` = travel(`distance_m`, `nominal_speed_mps × crowd_factor`)
  + `n_stops × turn_penalty_s`. **Без dwell**: `EstimateRoute.srv` не
  несёт `dwell_s` по остановкам (это поле есть только в `TourStop`,
  которого сервис не видит) — время у экспоната прибавляет вызывающая
  сторона, если оно ей известно.
- TF недоступен, либо неизвестный `id` — `feasible=false`, `ordered_ids`
  = вход как есть, без попытки переупорядочить.

Дефект контракта `ComputeRoute.action` в nav2_route 1.1.20 (эмпирически
подтверждён, не гипотеза): коды ошибок объявлены как константы, но поля
под них в `result` нет. `route_planner` отличает «маршрута нет» от
«сервер недоступен» по статусу цели (`ABORTED` vs таймаут/отказ), не по
`result`.

**Параметры**: `locations_file`, `nominal_speed_mps=0.35`,
`crowd_factor=0.7`, `turn_penalty_s=3.0`, `tsp_time_budget_ms=200.0`,
`compute_route_call_timeout_s=2.0`, `route_server_wait_timeout_s=10.0`.

## Общее для всех трёх нод

- **Гард неактивного сервиса** (`service_guard.py`, `ServiceGuardMixin`):
  `create_service()` в rclpy не глушится сам по факту `INACTIVE` —
  только `create_lifecycle_publisher()`. Вызов сервиса вне `ACTIVE`
  логируется `ERROR`, шлёт `SystemEvent`, отдаёт response с полями по
  умолчанию — явная деградация, не тишина и не краш.
- **`on_configure` = полная валидация.** Любая ошибка данных —
  `FAILURE`, робот не поедет с битым контентом/локациями.
- Все три публикуют `/system_event` (`guide_robot_msgs/msg/SystemEvent`)
  через один и тот же QoS-профиль (`lib/qos.py` — единственный модуль в
  `lib/`, которому разрешено импортировать `rclpy`).

## Сервисы (сводно)

| Сервис | Тип | Нода |
|---|---|---|
| `~/get_exhibit_content` | `GetExhibitContent` | content_server |
| `~/list_locations` | `ListLocations` | location_server |
| `~/resolve_location` | `ResolveLocation` | location_server |
| `~/list_tours` | `ListTours` | location_server |
| `~/estimate_route` | `EstimateRoute` | route_planner |
| `/compute_route` (action) | `nav2_msgs/action/ComputeRoute` | route_server (клиент — route_planner) |

## Данные

```
config/
  graph.geojson       # топология для Route Server (GeoJsonGraphFileLoader)
  locations.yaml      # id -> (x, y, yaw), zone, category, aliases, graph_node
  tours.yaml          # предустановленные туры, стопы -> location_id + exhibit_id
  semantic_map.yaml   # ros-параметры трёх нод
content/
  <exhibit_id>.<lang>.yaml   # чанки текста, chunks[].level in {short, full}
```

`graph.geojson` — GeoJSON `FeatureCollection`: узлы — `Point`-фичи,
рёбра — фичи с `LineString`/`MultiLineString` (оба варианта проверены
на реальном `nav2_route 1.1.20`, оба работают одинаково). `id` узлов и
рёбер — целые в `[0, 65535]` (`ComputeRoute.start_id`/`goal_id` — uint16).

**Данные лаборатории в репозитории сейчас — placeholder.** 7 локаций
(`entrance` + 6 экспонатов лаборатории), координаты — простая цепочка
с шагом 2 м по прямой, помечены `metadata.placeholder: true` в
`graph.geojson` и комментарием в начале `locations.yaml`. Настоящие
координаты появятся после трассировки карты лаборатории в
`slam_toolbox`/RViz — тогда оба файла обновляются вместе (`graph_node`
в `locations.yaml` обязателен и не может быть `null`: битая/отсутствующая
ссылка — ошибка данных, а не деградация).

**Дыры в контенте**, тоже намеренные: `content/` не содержит текста для
`intro` (остановка `entrance` в туре `lab_demo`) и для `claude_code_ros2_kit`
— это реальные экскурсоводческие тексты, которых пока никто не написал,
и `content_server` их не выдумывает (см. выше).

## `lib/` — логика без rclpy

Всё, что тестируется без поднятого ROS: парсинг/валидация данных,
резолв алиасов, TSP, оценка времени, геометрия.

| Модуль | Что делает |
|---|---|
| `graph_io.py` | Парсинг и валидация `graph.geojson` |
| `locations_io.py` | `locations.yaml`/`tours.yaml`, кросс-валидация, `is_visible`, `filter_near` |
| `text_norm.py` | Нормализация ru/en для сравнения алиасов (NFC, ё→е, стоп-слова) |
| `matching.py` | Fuzzy-резолв: точное совпадение → префикс → `difflib` |
| `content_io.py` | `content/*.yaml`, выбор чанков по `mode`, выбор языка |
| `tsp.py` | Открытый TSP с фиксированным стартом: Held-Karp / эвристика |
| `estimate.py` | `distance_m` → `duration_min` |
| `geometry.py` | `yaw` ↔ кватернион, длина плотного пути |
| `qos.py` | QoS-профиль `/system_event` (единственное исключение из «без rclpy») |

## Запуск

```bash
# Всё, ноды unconfigured -- подъём вручную или через mission
ros2 launch guide_robot_semantic_map semantic_map.launch.py

# Всё, автоподъём в порядке route_server -> content_server ->
# location_server -> route_planner (route_planner на activate ждёт
# route_server и сразу прогревает матрицу пар)
ros2 launch guide_robot_semantic_map semantic_map.launch.py autostart:=true
```

Ручной подъём (если `autostart:=false`):

```bash
ros2 lifecycle set /route_server configure
ros2 lifecycle set /route_server activate
ros2 lifecycle set /content_server configure
ros2 lifecycle set /content_server activate
ros2 lifecycle set /location_server configure
ros2 lifecycle set /location_server activate
ros2 lifecycle set /route_planner configure
ros2 lifecycle set /route_planner activate
```

Проверка руками:

```bash
ros2 service call /content_server/get_exhibit_content \
    guide_robot_msgs/srv/GetExhibitContent \
    "{exhibit_id: nav2_course, mode: short, language: ru}"

ros2 service call /location_server/resolve_location \
    guide_robot_msgs/srv/ResolveLocation \
    "{query: лидар, language: ru, max_results: 3}"

# near_only и первая лега estimate_route требуют живого TF map->base_link
ros2 run tf2_ros static_transform_publisher --x 0 --y 0 --z 0 \
    --frame-id map --child-frame-id base_link

ros2 service call /route_planner/estimate_route \
    guide_robot_msgs/srv/EstimateRoute \
    "{ids: [entrance, nav2_course, livox_mid70], optimize: true}"
```

## Известные грабли

- **`ComputeRoute.use_poses` переключает и `start`, и `goal` разом.**
  Не только `start` — если `use_poses=true`, `goal` тоже обязан прийти
  как `PoseStamped`, а не `goal_id`; иначе route_server пытается
  трансформировать пустую (`frame_id=''`) позу и роняет цель с
  `ABORTED`. Обнаружено эмпирически при первой реальной проверке
  route_server (design предполагал только про `start`).
- **`use_start=false` игнорирует переданное поле `start` целиком** —
  route_server сам делает TF-подстановку внутри, независимо от того,
  что (и есть ли) в `start`. `route_planner` поэтому не строит реальную
  позу для этой леги, а только проверяет доступность TF заранее
  (`_tf_available()`) — иначе на N живых вызовов ушёл бы одинаково
  бесполезный `start`.
- **`nav2_lifecycle_manager` и `bond`.** `route_server` — C++
  `nav2_util::LifecycleNode`, создаёт bond; наши три ноды — обычные
  `rclpy.lifecycle.LifecycleNode`, bond не создают. `bond_timeout: 0.0`
  в `semantic_map.launch.py` выключает проверку для группы целиком —
  смешанный состав иначе не поднять одним `node_names` (тот же приём в
  `guide_robot_voice`/`guide_robot_llm`).
- **Пакет не зарегистрирован в `guide_robot_supervisor`.** design.md
  требовал регистрации в `supervisor.yaml`; `guide_robot_voice` и
  `guide_robot_llm` — ближайшие прецеденты lifecycle-пакетов —
  сознательно этого не делают (`supervisor.yaml` жёстко про
  safety-critical нав-стек, bring-up сервисного слоя — дело
  ещё не реализованного mission-оркестратора). Решено следовать
  прецеденту, не тексту design.md — см. «Отличия от design» ниже.
- **`route_server` больше нигде в репозитории не запускается.**
  `semantic_map.launch.py` поднимает его сам (данные — `graph.geojson`
  — живут в этом пакете), а не `guide_robot_navigation`, где обычно
  собраны остальные Nav2-компоненты.

## Тесты

```bash
cd guide_robot_semantic_map
python3 -m pytest test/ -q
ruff check .
```

164 юнит-теста на `lib/` и на реальных `config/`/`content/` разом
(`test_config_data.py` — данные лаборатории проходят через весь стек
валидаторов, не только фикстуры), без ROS. Ноды (`*_server.py`,
`route_planner.py`) юнитами не покрыты сознательно — design разносит
верификацию на реальный `route_server`/`ros2 lifecycle`/`ros2 service
call` вручную (см. `guide_robot_semantic_map_design.md` §5);
интеграционная проверка проведена во время реализации на живом
`nav2_route 1.1.20` в контейнере, но не автоматизирована.

## Отличия от `guide_robot_semantic_map_design.md`

Кроме семи пунктов из design §0 (уже учтены в тексте выше, где
применимо), при реализации накопились ещё:

1. **`cache.py` не реализован.** Design §3 перечисляет его в дереве
   `lib/`, но design §0.7 явно отменяет дисковый кэш матрицы целиком
   («Ни кэша, ни sha1 карты, ни assume_symmetric») — это рассинхрон
   внутри самого документа, а не пропуск: матрица кэшируется в памяти
   на `on_activate` (см. `route_planner`), диска не касается.
2. **`ComputeRoute.use_poses` гейтит `start` и `goal` вместе**, не
   только `start` — см. «Известные грабли».
3. **Не зарегистрирован в `guide_robot_supervisor`** — см. «Известные
   грабли». Design §4 п.6 буквально требовал регистрации; решение
   принято по образцу `guide_robot_voice`/`guide_robot_llm`, а не по
   тексту design.
4. **Placeholder-координаты лаборатории приняты сознательно**, не
   оставлены `null`: design §0.5 требует отказа активации на битую
   ссылку `graph_node`, а исходные черновики (`locations_lab_demo.yaml`)
   держали `graph_node: null` до трассировки карты. Решено подставить
   согласованные placeholder-значения в оба файла (`graph.geojson` +
   `locations.yaml`), чтобы весь стек был живым и тестируемым уже
   сейчас, а не ждал реальной геометрии.
