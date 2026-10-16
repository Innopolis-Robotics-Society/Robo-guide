# guide_robot_semantic_map

Read-only заземление робота-гида: единственный источник истины по «где
что» и «что про это можно сказать». Две `rclpy.lifecycle.LifecycleNode`.
Пакет ничего не пишет, ничего не генерирует, не знает ни про FSM тура, ни
про LLM — только отдаёт то, что уже лежит в `config/`/`content/`.
Маршрутов не считает: `route_server`/`route_planner` убраны (~0.3 ядра
Jetson в простое, а туры их не использовали) — тур едет от точки к точке
через Nav2 `navigate_to_pose` прямо из mission.

`ament_python`, ROS 2 Humble. Практический справочник по факту реализации

## Топология

```
                     ┌─────────────────┐
   config/*.yaml ──► │ content_server  │──/system_event (языковой фолбэк)
                     └─────────────────┘
                      ~/get_exhibit_content, ~/search_content

                     ┌─────────────────┐
   config/*.yaml ──► │ location_server │◄── TF map->base_link (near_only)
                     └─────────────────┘
                ~/list_locations, ~/resolve_location,
                ~/list_tours

Вызывающая сторона (mission/LLM-слой)
```

Каждая нода читает свои данные независимо на `on_configure`; между собой
ноды не разговаривают. `locations.yaml`/`tours.yaml` — позы, зоны,
категории, алиасы, туры.

## Ноды

### `content_server`

Загружает `content/*.yaml` целиком в память на `on_configure` — никакого
чтения с диска в рантайме. Ни одной кодовой ветки, порождающей текст:
нет файла для `exhibit_id`/языка — пустой `chunks[]` и `version=""`,
решение (переспросить, промолчать) принимает вызывающая сторона.

**Сервисы**:
- `~/get_exhibit_content` (`GetExhibitContent`) —
  `exhibit_id, mode, language` → `chunks: ExhibitChunk[], title, kind, version`.
  Каждый `ExhibitChunk` — `chunk_id, text, interruptible, pause_after_s`
  (stage4 §1.2 — раньше `chunks`/`chunk_ids` были параллельными
  `string[]`, теперь `chunk_id` внутри самого элемента). `interruptible`
  (default `true`) гейтит автоматический VAD-барж-ин на этом чанке
  (`guide_robot_voice/vad_node.py`, `SpeakingStatus.interruptible`) —
  стоп-слово и e-stop им не гейтятся. `pause_after_s` (default `0.0`,
  диапазон `[0, 15]`) — тишина после чанка до следующего, барж-ин во
  время неё разрешён независимо от флага следующего чанка
  (`narration_server_node.py`).
- `~/search_content` (`SearchContent`) — `query, language, location_ids,
  kinds, max_results` → `hits: ContentHit[]`. BM25 (`lib/search.py`,
  перенесён из бывшего `guide_robot_llm/kb/retriever.py`) по одному индексу
  на язык, единица индекса — чанк (`content_id, chunk_id`), не файл целиком.
  `location_ids` — буст (`score *= 1.5`), не фильтр; `kinds` — фильтр (пусто
  = все). `max_results`: `0` → default `5`, максимум `20`. Абсолютного
  `min_score` по-прежнему нет (порог на малом корпусе не настраивался), но
  хиты без ни одного общего терма с запросом отбрасываются ДО обрезки по
  `max_results` (stage2 A3, живой баг: «стоп» → 8 хитов со score 0.0) —
  проверка по пересечению множеств токенов, не по знаку скора (классический
  BM25 без сглаживания idf уходит в минус для частого в маленьком корпусе
  терма).

Каждый элемент `content/*.yaml` несёт `kind` (`exhibit|place|city|org|
transit`, по умолчанию `exhibit`) и `location_ids` (локации, к которым
привязан текст; по умолчанию `[exhibit_id]` для `kind: exhibit`, иначе
пусто) — используются для буста поиска и фильтра по типу текста.
`location_ids`, которых нет в `locations.yaml`, — не отказ активации, а
`WARN`-лог + `SystemEvent` (`semantic_map.content_unknown_location_id`):
контент про город/организацию вправе ссылаться на зону, а не на локацию.

Фолбэк языка: запрошенный → `default_language` → пусто. Молчаливая
подмена запрещена — факт фолбэка идёт `WARN`-логом и `SystemEvent`
(`semantic_map.content_language_fallback`).

**Параметры**: `content_dir` (пусто → `share/guide_robot_semantic_map/content`),
`locations_file` (пусто → `share/.../config/locations.yaml`, только для
WARN-проверки `location_ids` выше), `default_language="ru"`.

### `location_server`

Читает `locations.yaml` + `tours.yaml`. Графа не грузит; устаревшую
ссылку локации на узел графа, если она встретится в данных, парсер
молча игнорирует.

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
  языком названия тура. `Tour.transit_content_id` (stage2 блок E) —
  опциональный `content_id` транзитного нарратива тура (`kind: transit`),
  пусто по умолчанию; `guide_robot_mission_control` читает его отсюда,
  так как своего чтения `tours.yaml` у него нет.
  `Tour.duration_min_estimate` всегда `0` (оценки маршрута больше нет).

`near_only=true` сортирует по евклидову расстоянию от TF `map →
base_link`, отсекает по `near_radius_m`. Если TF недоступен — `WARN` +
`SystemEvent`, но список **не** обнуляется: отдаётся без фильтра по
расстоянию (локации валидны независимо от TF, посчитать расстояние не
вышло — это не повод скрыть данные).

**Параметры**: `locations_file`, `tours_file` (пустые
→ `share/.../config/*`), `default_language="ru"`, `near_radius_m=5.0`.

## Общее для обеих нод

- **Гард неактивного сервиса** (`service_guard.py`, `ServiceGuardMixin`):
  `create_service()` в rclpy не глушится сам по факту `INACTIVE` —
  только `create_lifecycle_publisher()`. Вызов сервиса вне `ACTIVE`
  логируется `ERROR`, шлёт `SystemEvent`, отдаёт response с полями по
  умолчанию — явная деградация, не тишина и не краш.
- **`on_configure` = полная валидация.** Любая ошибка данных —
  `FAILURE`, робот не поедет с битым контентом/локациями.
- Обе публикуют `/system_event` (`guide_robot_msgs/msg/SystemEvent`)
  через один и тот же QoS-профиль (`lib/qos.py` — единственный модуль в
  `lib/`, которому разрешено импортировать `rclpy`).

## Сервисы (сводно)

| Сервис | Тип | Нода |
|---|---|---|
| `~/get_exhibit_content` | `GetExhibitContent` | content_server |
| `~/search_content` | `SearchContent` | content_server |
| `~/list_locations` | `ListLocations` | location_server |
| `~/resolve_location` | `ResolveLocation` | location_server |
| `~/list_tours` | `ListTours` | location_server |

## Данные

```
config/
  locations.yaml      # id -> (x, y, yaw), zone, category, aliases
  tours.yaml          # предустановленные туры, стопы -> location_id + exhibit_id
  semantic_map.yaml   # ros-параметры обеих нод
content/
  <exhibit_id>.<lang>.yaml   # чанки текста, chunks[].level in {short, full},
                             # chunks[].interruptible/pause_after_s (stage4 §1.1)
```

**Координаты лаборатории** в `locations.yaml` сняты по реальной карте
(`guide_robot_navigation/map/lab_105_full.pgm`); `yaw` пока не измерен и
везде `0.0` — заменить на реальную ориентацию «лицом к экспонату» по
месту (комментарий в начале файла).

**Дыры в контенте закрыты для `lab_demo`:** все шесть остановок тура,
включая `intro` (остановка `entrance`), покрыты `content/*.yaml` —
`content_server` по-прежнему ничего не выдумывает (см. выше), но текстов,
которых раньше не было, больше нет ни для одной остановки этого тура.

**`expo_one` (stage4) — сценическая экскурсия по `expo_one.md`, три
остановки (`expo_meeting` → `expo_city_model` → `expo_handoff`), позы
которых — тоже заглушки (скопированы с `entrance`/`robo_guide`/
`promobot_m13_artist`, `# TODO(Mook)` в `locations.yaml`).
`confirm_between_stops`/`greet`/`return_home` — не поля тура (в
`tours.yaml` их нет), это аргументы `RunTour.Goal` у вызывающего; живой
запуск шоу — `ros2 run guide_robot_mission_control cli tour --tour
expo_one --no-greet --no-confirm` (`--no-return-home` НЕ передавать —
финальный отъезд должен быть штатным `RETURNING`).**

## `lib/` — логика без rclpy

Всё, что тестируется без поднятого ROS: парсинг/валидация данных,
резолв алиасов, поиск, геометрия.

| Модуль | Что делает |
|---|---|
| `locations_io.py` | `locations.yaml`/`tours.yaml`, кросс-валидация, `is_visible`, `filter_near` |
| `text_norm.py` | Нормализация ru/en для сравнения алиасов (NFC, ё→е, стоп-слова) |
| `matching.py` | Fuzzy-резолв: точное совпадение → префикс → `difflib` |
| `content_io.py` | `content/*.yaml`, выбор чанков по `mode`, выбор языка |
| `search.py` | BM25-поиск по чанкам контента |
| `geometry.py` | `yaw` → кватернион |
| `qos.py` | QoS-профиль `/system_event` (единственное исключение из «без rclpy») |

## Запуск

```bash
# Всё, ноды unconfigured -- подъём вручную или через mission
ros2 launch guide_robot_semantic_map semantic_map.launch.py

# Всё, автоподъём в порядке content_server -> location_server
ros2 launch guide_robot_semantic_map semantic_map.launch.py autostart:=true
```

Ручной подъём (если `autostart:=false`):

```bash
ros2 lifecycle set /content_server configure
ros2 lifecycle set /content_server activate
ros2 lifecycle set /location_server configure
ros2 lifecycle set /location_server activate
```

Проверка руками:

```bash
ros2 service call /content_server/get_exhibit_content \
    guide_robot_msgs/srv/GetExhibitContent \
    "{exhibit_id: nav2_course, mode: short, language: ru}"

ros2 service call /location_server/resolve_location \
    guide_robot_msgs/srv/ResolveLocation \
    "{query: лидар, language: ru, max_results: 3}"

# near_only требует живого TF map->base_link
ros2 run tf2_ros static_transform_publisher --x 0 --y 0 --z 0 \
    --frame-id map --child-frame-id base_link

ros2 service call /location_server/list_locations \
    guide_robot_msgs/srv/ListLocations "{near_only: true}"
```

## Известные грабли

- **`nav2_lifecycle_manager` и `bond`.** Обе ноды — обычные
  `rclpy.lifecycle.LifecycleNode`, bond не создают. `bond_timeout: 0.0`
  в `semantic_map.launch.py` выключает проверку bond (тот же приём в
  `guide_robot_voice`/`guide_robot_llm`).
- **Пакет зарегистрирован в `guide_robot_supervisor`** — группа
  `semantic_map` в `config/supervisor.yaml`/`config/supervisor_slam.yaml`
  (`optional: true`). `guide_robot_llm` остаётся вне `supervisor.yaml`
  (там ещё не реализованный mission-оркестратор был единственным
  прецедентом на момент написания design §4 п.6), но с появлением
  `guide_robot_mission_control` регистрация service-слоя (`voice`/
  `semantic_map`/`mission`, mission требует navigation+voice+semantic_map)
  реализована буквально по design — см. «Отличия от design» ниже и
  `guide_robot_mission_control/README.md`. `lifecycle_manager_semantic_map`
  запускается безусловно (не под `IfCondition(autostart)`) в
  `semantic_map.launch.py` — сервис `~/manage_nodes` обязан существовать
  для супервизора вне зависимости от того, кто инициирует `STARTUP`.

## Тесты

```bash
cd guide_robot_semantic_map
python3 -m pytest test/ -q
ruff check .
```

Юнит-тесты на `lib/` и на реальных `config/`/`content/` разом
(`test_config_data.py` — данные лаборатории проходят через весь стек
валидаторов, не только фикстуры), без ROS. Ноды (`*_server.py`)
юнитами не покрыты сознательно — design разносит верификацию на
`ros2 lifecycle`/`ros2 service call` вручную (см.
`guide_robot_semantic_map_design.md` §5).

## Отличия от `guide_robot_semantic_map_design.md`

Кроме семи пунктов из design §0 (уже учтены в тексте выше, где
применимо), при реализации накопились ещё:

1. **Маршрутного слоя нет** (design §0.5/§0.7 его предполагал) — убран,
   см. начало README.
2. **Зарегистрирован в `guide_robot_supervisor`** (группа `semantic_map`,
   `optional: true`) — design §4 п.6 требовал этого буквально; реализовано
   позже, вместе с регистрацией `guide_robot_mission_control` — см.
   «Известные грабли».
