# guide_robot_semantic_map — проект пакета

`ament_python`, ROS 2 Humble. Read-only заземление: единственный источник истины по «где что»
и «что про это можно сказать». Ничего не пишет, ничего не генерирует, не знает о FSM и LLM.

---

## 0. Расхождения с ТЗ

Семь пунктов. Первые три — блокирующие, остальные — уточнения контракта.

### 0.1 `graph.yaml` → `graph.geojson`

Nav2 Route Server парсит граф плагином `GeoJsonGraphFileLoader`
(`graph_file_loader: "GeoJsonGraphFileLoader"`, `plugin: nav2_route::GeoJsonGraphFileLoader`).
YAML-парсера в апстриме нет и не планируется. В README `nav2_route` YAML-пример приведён
явно с оговоркой «GeoJSON не YAML-based, ниже — для читаемости».

Хранить граф в YAML = гарантированно писать свой `GraphFileLoader`-плагин на C++ при миграции,
то есть ровно та переделка, которой мы хотели избежать. Файл — `config/graph.geojson`.

Обязательный минимум по спеке Route Server:
- у узлов и рёбер уникальный `id`;
- у ребра `startid` / `endid`;
- у узла `coordinates`.

Рекомендуется: `frame` у узла, `cost` и `overridable` у ребра. Всё остальное — под ключом
`metadata`, произвольное, доезжает до edge scorer'ов и route operations без потерь.

Зарезервированные апстримом ключи метаданных, которые нам пригодятся сразу:
`speed_limit` (% от максимума), `abs_speed_limit` (м/с), `penalty`, `class` (семантический класс
для `SemanticScorer`), `abs_time_taken`. Занимать их своими смыслами нельзя.

### 0.2 RViz Route Tool в Humble отсутствует, но сервер полноценный

`nav2_route` зарелижен под Humble: 1.1.20, ветка `humble`, у тебя стоит
`1.1.20-1jammy.20260607.083243`. Моя первоначальная посылка «это фича Jazzy+» была неверна.

Набор плагинов в 1.1.20 по `plugins.xml` в сборке — полный, отставания от Jazzy нет:
все восемь edge scorer'ов (`DistanceScorer`, `DynamicEdgesScorer`, `PenaltyScorer`,
`CostmapScorer`, `SemanticScorer`, `TimeScorer`, `GoalOrientationScorer`,
`StartPoseOrientationScorer`), пять route operations (`AdjustSpeedLimit`, `ReroutingService`,
`TriggerEvent`, `CollisionMonitor`, `TimeMarker`), `GeoJsonGraphFileLoader`
и `GeoJsonGraphFileSaver`.

Отсутствует только RViz-панель: в humble-ветке `nav2_rviz_plugins` не зависит от `nav2_route`
(в Jazzy и Kilted — зависит). Это инструмент редактирования, а не сервер.

Имена сверять по `plugins.xml`, а не по docs.nav2.org (там документация main). Уже разошлось:
в 1.1.20 скорер называется `GoalOrientationScorer`, в документации — `GoalPoseOrientationScorer`.

Чем редактируем граф:

- **VDA5050 LIF Editor** (web, open-source) — подложка из картинки карты, `Export ROS GeoJSON`
  плюс `.lif` как редактируемый исходник рядом в git. Рекомендую: ставить ничего не надо.
- Свой скрипт: `/clicked_point` → узел, `nav2_msgs/srv/SetRouteGraph` для горячей подмены графа
  без перезапуска сервера. `GeoJsonGraphFileSaver` в сборке есть, но `SaveRouteGraph.srv`
  в списке интерфейсов отсутствует — сейвер, вероятно, используется `TimeMarker`'ом для
  персиста `abs_time_taken`. Проверить `ros2 service list | grep route_server` на живом сервере.
- QGIS по апстрим-туториалу — оверкилл для 20–30 узлов.

### 0.3 Трёх сервисов в `guide_robot_msgs` не хватает

Существующие контракты:

```
ListLocations.srv       zone, category, near_only            → Location[]
EstimateRoute.srv       ids[], optimize                      → ordered_ids[], distance_m, duration_min, feasible
GetExhibitContent.srv   exhibit_id, mode, language           → chunks[], version
Location.msg            id, aliases[], pose, zone, category, is_public
```

Дыры:

1. **Fuzzy-match алиасов негде вызвать.** В `ListLocations` нет строки запроса. Либо резолв
   уезжает на сторону клиента (LLM получает весь список и выбирает — лишние токены, недетерминированно,
   ломает инвариант «заземление здесь»), либо нужен новый сервис.
2. **`tours.yaml` не отдаётся наружу.** `RunTour.action` принимает `tour_id`, значит mission
   должен уметь развернуть его в список остановок. Читать чужой YAML из mission — дублирование
   источника истины.
3. **`Location.aliases` без языка**, а `ListLocations` без поля `language`. Отдавать объединение
   ru+en — единственный вариант при текущем контракте.

Предлагаю добавить в `guide_robot_msgs` (это следующий шаг, до кода semantic_map):

```
# srv/ResolveLocation.srv
string query
string language          # ru|en, пусто = все
uint8 max_results        # 0 = default (5)
---
Location[] candidates
float32[] scores         # 0..1, убыванию
bool confident           # scores[0] >= threshold и отрыв от scores[1] >= margin
```

```
# srv/ListTours.srv
string language
---
Tour[] tours
```

```
# msg/Tour.msg
string id
string name
uint32 duration_min_estimate
TourStop[] stops
```

```
# msg/TourStop.msg
string location_id
string exhibit_id
uint32 dwell_s
string mode              # short|full
```

`confident` в `ResolveLocation` — чтобы mission мог решать «веду сразу» vs «переспрашиваю через
`AskUser`», не завися от порога, зашитого в LLM-промпт.

### 0.4 `EstimateRoute` не принимает стартовую позу

При `optimize=true` задача — открытый path-TSP с фиксированным стартом, старт = текущая поза робота.
Берём из TF `map → base_link` внутри `route_planner`. Возврат в старт не делаем
(параметр `return_to_start: false`). Если TF недоступен — `feasible=false`, `ordered_ids` = вход как есть.

### 0.5 Ориентация живёт в `locations.yaml`, не в графе

Узел Route Server — только координаты, без yaw. А «встать лицом к экспонату» — это yaw.
Поэтому разделение, которое ты заложил, обязательно, а не косметическое:

- `graph.geojson` — топология и проходимость (что Route Server поймёт нативно);
- `locations.yaml` — `id → (x, y, yaw)`, зона, категория, алиасы, привязка к узлу графа.

Связь — поле `graph_node` в `locations.yaml`. Валидируется на старте: битая ссылка = отказ активации.

`id` узлов и рёбер — `uint16` (`ComputeRoute.start_id` / `goal_id`). Значит идентификаторы
в `graph.geojson` целые и ≤ 65535, строковых ID быть не может. Человекочитаемое имя узла
живёт в `metadata.name` и в `locations.yaml`.

### 0.6 Чанки контента получают поле `level`

`GetExhibitContent.mode ∈ {short, full}` нечем реализовать, если чанки — плоский список.
Каждый чанк помечается `level: short|full`; `short` — подмножество, отдаётся в порядке файла.
`mode=full` отдаёт всё.

### 0.7 Матрица считается через `ComputeRoute`, а не `ComputePathToPose`

Исходный план — N² вызовов `ComputePathToPose`, дисковый кэш, фоновый прогрев, симметричное
допущение — отменяется целиком. `route_server` доступен на Humble, а поиск по графу занимает
микросекунды против сотен миллисекунд у free-space планировщика. 30 локаций = 870 направленных
пар, ~1–2 мс на ROS-роундтрип, итого секунды на `on_activate`. Ни кэша, ни sha1 карты,
ни `assume_symmetric`.

---

## 1. Ноды

Все три — `LifecycleNode`. `on_configure` = загрузка + полная валидация данных (при любой ошибке
`FAILURE`, робот не поедет с битым контентом). `on_activate` = сервисы начинают отвечать.
Порядком владеет `guide_robot_supervisor`.

Сервисы в неактивном состоянии обязаны отвечать явной ошибкой, а не молчать — rclpy сам их
не глушит. Общий гард в базовом миксине.

### 1.1 `location_server`

| Сервис | Тип |
|---|---|
| `~/list_locations` | `ListLocations` |
| `~/resolve_location` | `ResolveLocation` *(новый)* |
| `~/list_tours` | `ListTours` *(новый)* |

Читает `locations.yaml`, `tours.yaml`, `graph.geojson` (только для валидации ссылок).
`near_only=true` требует TF `map → base_link`: сортировка по евклидову расстоянию, отсечка по
параметру `near_radius_m`. Евклид, не путевое расстояние — путевое считает `route_planner`,
дублировать зависимость от Nav2 в этой ноде не надо.

Фильтрация: `is_public=false` не отдаётся никогда, кроме `category`-запроса ровно за служебной
категорией (`charging`, `service`) — чтобы LLM не мог случайно предложить посетителю подсобку.

**Резолв алиасов** — `lib/matching.py`, без внешних зависимостей:

1. нормализация: lowercase, `ё→е`, `й→й` (NFC), схлопывание пунктуации и пробелов, отбрасывание
   стоп-слов (`к`, `на`, `в`, `у`, `где`, `покажи`, `отведи`, `хочу`);
2. точное совпадение по нормализованной форме → score 1.0, короткое замыкание;
3. совпадение по префиксу словоформы длиной ≥ 4 (грубая замена стеммингу для русского);
4. `difflib.SequenceMatcher.ratio()` как добивка.

`rapidfuzz` даёт лучше и быстрее, но это лишний rosdep на Orin ради 30 строк. Если понадобится —
подменяется за одним импортом, интерфейс `score(query, candidate) -> float` тот же.

Инвариант валидации: две локации не могут иметь совпадающий нормализованный алиас в пределах
одного языка. Это ошибка данных, ловится на `configure`, а не на посетителе.

### 1.2 `route_planner`

| Сервис | Тип |
|---|---|
| `~/estimate_route` | `EstimateRoute` |

Клиент `nav2_msgs/action/ComputeRoute`. Контракт в 1.1.20:

```
# goal
uint16 start_id
geometry_msgs/PoseStamped start
uint16 goal_id
geometry_msgs/PoseStamped goal
bool use_start      # false → старт берётся из TF
bool use_poses      # false → используются start_id/goal_id
---
# result
builtin_interfaces/Duration planning_time
nav_msgs/Path path
Route route
```

Для матрицы: `use_poses=false`, пара `start_id`/`goal_id` из поля `graph_node` локаций.
Для леги «текущая поза → первая остановка»: `use_poses=true`, `use_start=false` — сервер сам
возьмёт позу робота из TF, отдельный TF-листенер в ноде не нужен (правка к п. 0.4).

**Два разных числа, которые нельзя путать.** `Route.route_cost` — сумма скоров рёбер, а не метры.
Совпадает с метрами только при единственном `DistanceScorer` с `weight: 1.0` и отсутствии
`speed_limit` в метаданных. Как только добавится `PenaltyScorer` или `SemanticScorer` — а они
добавятся, для обхода служебных зон и приоритета широких проходов — `route_cost` перестанет
быть длиной.

Поэтому:

- **`route_cost` → целевая функция TSP.** Это правильный критерий: он уже учитывает штрафы
  и семантику, то есть оптимизируем то, что действительно хотим минимизировать.
- **`distance_m` → длина `nav_msgs/Path`**, сумма евклидовых отрезков плотного пути.
  Плотность задаётся параметром сервера `path_density`.

Оба приходят в одном ответе, лишних вызовов нет.

**Дефект контракта в 1.1.20.** В `ComputeRoute.action` объявлены константы кодов ошибок
(`NO_VALID_GRAPH=402`, `INDETERMINANT_NODES_ON_GRAPH=403`, `TIMEOUT=404`, `NO_VALID_ROUTE=405`
и остальные), но **поля под них в result нет** — константы висят без носителя. Отличить
«граф не загружен» от «маршрута не существует» по результату невозможно. Обрабатываем
по статусу goal'а (`ABORTED`) плюс логи сервера; в `SystemEvent` кладём текстовый `detail`,
а не код. Чинить локальным форком `nav2_msgs` не стоит — потянет пересборку половины nav2.

`RouteEdge` содержит только `edgeid`, `start`, `end`; стоимость отдельного ребра наружу
не выдаётся, доступен лишь агрегат `route_cost`. Для матрицы этого достаточно.

**TSP.** Открытый путь, фиксированный старт (текущая поза), без возврата:
- N ≤ 12 → точный Held–Karp (2¹² · 12² ≈ 6·10⁵ операций, единицы мс);
- N > 12 → nearest-neighbour + 2-opt + or-opt до сходимости или бюджета `tsp_time_budget_ms`.

Матрица направленная и считается честно в обе стороны: граф Route Server ориентированный,
односторонние проходы (турникеты, узкие галереи) выражаются штатно.

`optimize=false` → порядок как пришёл, только оценка.

**Оценка времени:**

```
duration_min = Σ(leg_dist / v_eff) + Σ dwell_s + n_stops · turn_penalty_s
v_eff = nominal_speed_mps · crowd_factor
```

Параметры: `nominal_speed_mps: 0.35`, `crowd_factor: 0.7`, `turn_penalty_s: 3.0`.
Музей с толпой — оценка должна быть пессимистичной, недооценка ломает `time_budget_min`
в `RunTour`.

Заготовка на будущее: `TimeMarker` пишет фактическое время проезда ребра в `abs_time_taken`,
`TimeScorer` его читает. Это лучше эвристики с `crowd_factor`, но требует накопленной
статистики — включать после нескольких недель эксплуатации, не на старте.

`feasible=false`, если хоть одна лега вернула `ABORTED`. Бюджет времени здесь не проверяется —
в `EstimateRoute` нет такого поля; обрезкой тура по бюджету занимается mission, вызывая
`EstimateRoute` итеративно.

**Зависимости.** Только `route_server`. Ни `planner_server`, ни глобальная костмапа не нужны,
пока не включён `CostmapScorer`. В порядке подъёма супервизора `route_planner` встаёт сразу
после `route_server`, до полного нав-стека.
`EstimateRoute` итеративно.

### 1.3 `content_server`

| Сервис | Тип |
|---|---|
| `~/get_exhibit_content` | `GetExhibitContent` |

Загружает `content/*.yaml` целиком в память на `configure` (объём — десятки КБ).
Никакого чтения с диска в рантайме: диск на Orin — это latency и точка отказа.

Отдаёт `chunks[]` и `version`. `version` кладётся в лог и в `NarrationToken` — по нему потом
восстанавливается, какая именно ревизия текста была произнесена.

Фолбэк языка: запрошенный → `default_language` → ошибка. Молчаливая подмена языка запрещена,
факт фолбэка идёт в `SystemEvent` severity `WARN`.

**Инвариант:** ни одной кодовой ветки, порождающей текст. Ни шаблонов, ни конкатенации фраз,
ни «если контента нет, скажи что-нибудь общее». Нет файла — пустой `chunks[]` и `version=""`,
решение принимает mission.

---

## 2. Данные

### `config/locations.yaml`

```yaml
version: 1
frame_id: map
locations:
  - id: entrance
    graph_node: 1
    pose: {x: 2.31, y: -0.44, yaw: 1.5708}
    zone: hall_a
    category: waypoint        # exhibit|waypoint|service|charging
    is_public: true
    exhibit_id: null
    aliases:
      ru: ["вход", "главный вход"]
      en: ["entrance", "main entrance"]

  - id: kandinsky_viii
    graph_node: 4
    pose: {x: 7.80, y: 3.15, yaw: -1.5708}
    zone: hall_a
    category: exhibit
    is_public: true
    exhibit_id: kandinsky_composition_viii
    aliases:
      ru: ["кандинский", "композиция восемь", "композиция viii"]
      en: ["kandinsky", "composition eight"]
```

`yaw` — куда робот развернётся, встав на точку. Для `category: exhibit` это «лицом к экспонату»,
проверяется глазами в симуляции, не вычисляется.

### `config/tours.yaml`

```yaml
version: 1
tours:
  - id: highlights_30
    name: {ru: "Обзорная, 30 минут", en: "Highlights, 30 min"}
    default: true
    stops:
      - {location_id: entrance,       exhibit_id: intro,                     dwell_s: 45,  mode: short}
      - {location_id: kandinsky_viii, exhibit_id: kandinsky_composition_viii, dwell_s: 120, mode: full}
```

Отступление от ТЗ: добавлено `mode` — иначе `RunTour` не знает, какую версию текста просить
для конкретной остановки, и короткий обзорный тур зачитает полные тексты.

### `content/<exhibit_id>.<lang>.yaml`

```yaml
exhibit_id: kandinsky_composition_viii
language: ru
version: "2026-08-04.1"
reviewed_by: "..."
reviewed_at: "2026-08-04"
title: "Композиция VIII"
chunks:
  - {id: c1, level: short, text: "Написана в 1923 году, в период работы Кандинского в Баухаусе."}
  - {id: c2, level: short, text: "Это одна из первых работ, где он полностью отказывается от фигуративности."}
  - {id: c3, level: full,  text: "..."}
```

Валидация на `configure`: непустой `text`, ≤ 3 предложений (нарушение — `WARN`, не отказ),
непустой `version`, уникальные `id` чанков, хотя бы один чанк уровня `short`.

### Где физически лежат данные

Дефолт — внутри пакета, чтобы `colcon build && ros2 launch` работал из коробки.
Параметр `data_dir` (default `$(find guide_robot_semantic_map)/`) перекрывается на развёртывании.
Реальные музейные тексты в кодовый репозиторий не кладём: у них свой цикл ревью, свой темп
изменений и не наша лицензия. Отдельный `guide_robot_venue_<name>` или просто каталог на машине.

---

## 3. Структура пакета

```
guide_robot_semantic_map/
├── package.xml
├── setup.py / setup.cfg
├── resource/guide_robot_semantic_map
├── guide_robot_semantic_map/
│   ├── __init__.py
│   ├── location_server.py
│   ├── route_planner.py
│   ├── content_server.py
│   └── lib/                    # ноль импортов rclpy — как в guide_robot_voice
│       ├── __init__.py
│       ├── graph_io.py         # чтение/валидация geojson
│       ├── locations_io.py     # locations.yaml + tours.yaml
│       ├── content_io.py       # content/*.yaml, модель чанка
│       ├── text_norm.py        # нормализация ru/en
│       ├── matching.py         # fuzzy resolve
│       ├── tsp.py              # held-karp + 2-opt, открытый путь
│       ├── estimate.py         # distance → duration
│       └── cache.py            # дисковый кэш матрицы
├── config/
│   ├── graph.geojson
│   ├── locations.yaml
│   ├── tours.yaml
│   └── semantic_map.yaml       # ros-параметры трёх нод
├── content/
│   └── *.yaml
├── launch/semantic_map.launch.py
└── test/
    ├── test_copyright.py
    └── test_*.py
```

`lib/` без `rclpy` — вся логика (резолв, TSP, оценка, валидация) тестируется в CI без ROS,
как в voice-пакете. После `ros2 pkg create` удалить `test_pep257.py` и `test_flake8.py`.

Зависимости: `rclpy`, `rclpy` lifecycle, `geometry_msgs`, `nav_msgs`, `nav2_msgs`, `tf2_ros`,
`guide_robot_msgs`, `python3-yaml`. GeoJSON — stdlib `json`, отдельная библиотека не нужна.

---

## 4. Порядок реализации

1. **`lib/`** целиком + юнит-тесты. Ноль ROS, всё проверяемо на фикстурах.
   Начинаем с `graph_io` + `locations_io` + валидатора перекрёстных ссылок.
2. **`content_server`** — самая простая нода, нет зависимостей от Nav2 и TF.
   Сразу даёт mission-у на чём тестироваться.
3. **`location_server`** — добавляется TF.
4. **`route_server`** — поднять штатный `nav2_route`, скормить `graph.geojson`, проверить
   `ComputeRoute` вручную по node ID через `ros2 action send_goal`. Чужой пакет, кода не пишем,
   но без работающего сервера следующий пункт не тестируется.
5. **`route_planner`** — action-клиент `ComputeRoute`, матрица, TSP.
6. **launch + параметры + регистрация обоих серверов в supervisor**.

Перед пунктом 1 — добавить `ResolveLocation.srv`, `ListTours.srv`, `Tour.msg`, `TourStop.msg`
в `guide_robot_msgs` и пересобрать. Иначе `location_server` придётся переписывать.

---

## 5. Тесты

**CI (без ROS):** валидация битых данных (дубль алиаса, висячая ссылка `graph_node`, отсутствующий
`version`, тур на несуществующую локацию); резолв алиасов на наборе реальных русских формулировок
включая опечатки; Held–Karp против брутфорса на N ≤ 8; корректность разделения
`route_cost` (порядок обхода) и `distance_m` (метры) на замоканных ответах `ComputeRoute`.

**Интеграционные (симуляция):** `estimate_route` на 8 локациях в Gazebo — сверка `distance_m`
с фактически пройденным путём; поведение при неподнятом `route_server` и при пустом
`graph_filepath` (различить их по результату нельзя — см. дефект `error_code`, проверяем,
что нода деградирует предсказуемо, а не виснет).
