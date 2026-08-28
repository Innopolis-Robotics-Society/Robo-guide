"""dialog_agent -- двухфазный ход поверх llm_client + tool_broker (DIALOG_REWORK_PLAN.md).

Отдельный процесс от `tool_broker` (см. `tool_broker_node.py:main()` --
`rclpy.init` -> один узел -> `spin()`, `llm.launch.py` запускает его своим
`Node(executable=...)`), поэтому `call_tool()` -- голый Python-метод --
недостижим напрямую: зовём через `~/call_tool` сервис
(`guide_robot_msgs/srv/CallTool.srv`), который `tool_broker_node.py`
предоставляет.

Кэш `/mission/state`/`/mission/presence` -- свой, отдельный от `tool_broker`
(разные процессы, разные подписки на один и тот же топик). Двухфазный ход
сам по себе -- чистая логика в `dialog/turn.py`, эта нода только собирает
вход (снимок + утверждение), инжектирует
`complete_answer`/`complete_action`/`speak`/`execute_tool` и реагирует на
ROS-события (транскрипт, переходы `/mission/state`, barge-in).

Локальный корпус знаний (`kb/`, `config/kb.jsonl`) убран
(CLAUDE_CODE_TASK_stage1_knowledge.md п.5): единственный источник фактов
про экспонаты/площадку/город -- `guide_robot_semantic_map/content/`, к
которому `dialog_agent` обращается через read-only инструменты
(`lookup_content`/`search_content`), а не через встроенный в системный
промпт текст.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future

from guide_robot_llm import matching, snapshot
from guide_robot_llm.dialog.history import DialogHistory
from guide_robot_llm.dialog.interaction_log import build_interaction_record
from guide_robot_llm.dialog.prompt import (
    build_action_instruction,
    build_answer_instruction,
    build_system_prompt,
)
from guide_robot_llm.dialog.sanitize import sanitize_answer
from guide_robot_llm.dialog.turn import (
    ToolCallRecord,
    TurnResult,
    render_action_outcome,
    run_turn,
)
from guide_robot_llm.lib.qos import (
    QOS_ASR_TRANSCRIPT,
    QOS_CANCEL_ALL,
    QOS_INTERACTION_EVENT,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
    QOS_WAKEWORD,
)
from guide_robot_llm.llm_client import Backend, BackendConfig, complete_with_fallback
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.tools import schema
from guide_robot_msgs.msg import (
    CancelAll,
    InteractionEvent,
    MissionState,
    Presence,
    Transcript,
    Wakeword,
)
from guide_robot_msgs.srv import CallTool

__all__ = ["DialogAgentNode", "main"]

_STATE_NAMES = {
    MissionState.STATE_IDLE: "IDLE",
    MissionState.STATE_GREETING: "GREETING",
    MissionState.STATE_NAVIGATING: "NAVIGATING",
    MissionState.STATE_NARRATING: "NARRATING",
    MissionState.STATE_ANSWERING: "ANSWERING",
    MissionState.STATE_AWAITING_CONFIRM: "AWAITING_CONFIRM",
    MissionState.STATE_PAUSED: "PAUSED",
    MissionState.STATE_HELD: "HELD",
    MissionState.STATE_RETURNING: "RETURNING",
}
_DEGRADED_REASONS = frozenset({"answer_backend_error", "action_backend_error", "aborted"})
_LISTEN_WINDOW_S = 8.0
_ACTIVATION_KEYWORDS = frozenset({"робот", "слушай робот"})


def _wait_future(future: Future, context: object, timeout_s: float) -> bool:
    """Дождаться future реальными миллисекундами -- копия хелпера из `tool_broker_node.py`.

    Пакет умышленно не делит этот код между модулями рантайм-импортом (тот
    же принцип, что у остальных "копия, не импорт" мест в этом пакете).
    """
    deadline = time.monotonic() + timeout_s
    while rclpy.ok(context=context):
        if future.done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.001)
    return False


@dataclass
class _RemoteToolResult:
    """Ответ `~/call_tool`, приведённый к форме `ToolResultLike` для `dialog/turn.py`."""

    ok: bool
    message: str
    data: dict


@dataclass
class _EmptyPresence:
    """Заглушка, пока /mission/presence ещё не пришёл ни разу."""

    present: bool = False
    seconds_since_evidence: float = 0.0


class DialogAgentNode(LifecycleNode):
    """Lifecycle-нода: двухфазный ход, слушает ASR/mission/cancel_all, зовёт tool_broker."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Бэкенды -- в `on_configure`, каталог -- в `on_activate`."""
        super().__init__("dialog_agent", **node_kwargs)

        self.declare_parameter("llm.base_urls", ["http://127.0.0.1:18080/v1"])
        self.declare_parameter("llm.connect_timeout_s", 2.0)
        self.declare_parameter("llm.read_timeout_s", 30.0)
        self.declare_parameter("llm.api_key", "")
        self.declare_parameter("llm.max_attempts_per_backend", 2)
        self.declare_parameter("llm.backoff_s", 0.5)
        self.declare_parameter("llm.max_tokens_answer", 160)
        self.declare_parameter("llm.max_tokens_action", 192)
        self.declare_parameter("llm.temperature_answer", 0.6)
        self.declare_parameter("llm.temperature_action", 0.0)
        self.declare_parameter("llm.action_repair_attempts", 1)
        self.declare_parameter("llm.raw", False)

        self.declare_parameter("system_prompt_path", "")
        self.declare_parameter("tool_broker_ns", "/tool_broker")
        self.declare_parameter("service_call_timeout_s", 2.0)
        self.declare_parameter("catalog_ns_timeout_s", 5.0)

        self.declare_parameter("history.max_entries", 16)
        self.declare_parameter("history.trim_to", 8)
        self.declare_parameter("history.cap_visitor_chars", 200)
        self.declare_parameter("history.cap_robot_chars", 300)
        self.declare_parameter("history.cap_event_chars", 120)
        self.declare_parameter("history.clear_after_absent_s", 25.0)

        self.declare_parameter("answer.max_chars", 400)
        self.declare_parameter("wake_grace_s", 30.0)

        self._active = False
        self._state_lock = threading.Lock()
        self._last_mission_state: MissionState | None = None
        self._last_presence: Presence | None = None

        self._turn_lock = threading.Lock()
        self._turn_in_flight = False
        self._abort_event: threading.Event | None = None
        self._turn_counter = 0
        # Слот отложенной реплики: транскрипт, пришедший пока ход в полёте,
        # не выбрасывается (живой баг «со второго раза»), а ждёт конца хода;
        # хранится только ПОСЛЕДНЯЯ реплика -- новое намерение побеждает.
        self._pending_text: str | None = None

        self._cb_reentrant = ReentrantCallbackGroup()

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять бэкенды/клиента/подписки/историю."""
        del state
        try:
            return self._configure()
        except Exception as error:
            self.get_logger().error(f"configure не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _configure(self) -> TransitionCallbackReturn:
        base_urls = list(self.get_parameter("llm.base_urls").value)
        connect_timeout_s = float(self.get_parameter("llm.connect_timeout_s").value)
        read_timeout_s = float(self.get_parameter("llm.read_timeout_s").value)
        api_key = str(self.get_parameter("llm.api_key").value)
        self._max_attempts_per_backend = int(
            self.get_parameter("llm.max_attempts_per_backend").value
        )
        self._backoff_s = float(self.get_parameter("llm.backoff_s").value)
        self._max_tokens_answer = int(self.get_parameter("llm.max_tokens_answer").value)
        self._max_tokens_action = int(self.get_parameter("llm.max_tokens_action").value)
        self._temperature_answer = float(self.get_parameter("llm.temperature_answer").value)
        self._temperature_action = float(self.get_parameter("llm.temperature_action").value)
        self._action_repair_attempts = int(
            self.get_parameter("llm.action_repair_attempts").value
        )
        self._raw_llm = bool(self.get_parameter("llm.raw").value)

        self._service_call_timeout_s = float(self.get_parameter("service_call_timeout_s").value)
        self._catalog_ns_timeout_s = float(self.get_parameter("catalog_ns_timeout_s").value)
        tool_broker_ns = str(self.get_parameter("tool_broker_ns").value)

        self._history_clear_after_absent_s = float(
            self.get_parameter("history.clear_after_absent_s").value
        )
        self._history = DialogHistory(
            max_entries=int(self.get_parameter("history.max_entries").value),
            trim_to=int(self.get_parameter("history.trim_to").value),
            cap_visitor=int(self.get_parameter("history.cap_visitor_chars").value),
            cap_robot=int(self.get_parameter("history.cap_robot_chars").value),
            cap_event=int(self.get_parameter("history.cap_event_chars").value),
        )
        self._told_ids: set[str] = set()
        self._listen_until = 0.0
        # stage2 A5: грейс-период без wakeword сразу после конца хода --
        # живой баг: "расскажи про себя" через 1с после stop_tour ушло в
        # "IDLE без wakeword, игнор", хотя разговор только что был.
        self._wake_grace_s = float(self.get_parameter("wake_grace_s").value)
        self._wake_grace_until = 0.0
        # Идентификатор сессии конфигурации: turn_id -- процессный счётчик,
        # без session_id перезапуск dialog_agent при живом interaction_log
        # переиспользует те же turn_id в том же jsonl-файле неотличимо от
        # дублей (живой баг: задвоенный turn=8 в логе).
        self._session_id = uuid.uuid4().hex[:12]

        self._answer_max_chars = int(self.get_parameter("answer.max_chars").value)

        self._backends = [
            Backend(
                BackendConfig(
                    base_url=url,
                    api_key=api_key,
                    connect_timeout_s=connect_timeout_s,
                    read_timeout_s=read_timeout_s,
                )
            )
            for url in base_urls
        ]

        # Преамбул -- из файла (та же копия должна греть
        # llm_server/config/system_prompt.txt). Полный системный промпт (с
        # каталогом локаций/туров) строится в on_activate -- каталог ещё не
        # пришёл на этапе configure (tool_broker может быть не активен).
        system_prompt_path = str(self.get_parameter("system_prompt_path").value)
        self._preamble = Path(system_prompt_path).read_text(encoding="utf-8")
        self._system_prompt = self._preamble
        self._action_instruction = ""
        self._answer_instruction = ""
        self._read_only_tool_names: frozenset[str] = frozenset()
        self._locations_catalog: list[dict] = []
        self._tours_catalog: list[dict] = []
        self._location_name_by_id: dict[str, str] = {}
        self._location_zone_by_id: dict[str, str] = {}
        self._tour_name_by_id: dict[str, str] = {}

        self._call_tool_client = self.create_client(
            CallTool, f"{tool_broker_ns}/call_tool", callback_group=self._cb_reentrant
        )
        # fire-and-forget: interaction_log может быть не запущен -- ход
        # диалога не обязан на него оглядываться (симметрично тому, что
        # tool_broker остаётся рабочим без dialog_agent).
        self._interaction_pub = self.create_publisher(
            InteractionEvent, "/dialog/interaction", QOS_INTERACTION_EVENT
        )

        self._mission_state_sub = self.create_subscription(
            MissionState,
            "/mission/state",
            self._on_mission_state,
            QOS_MISSION_STATE,
            callback_group=self._cb_reentrant,
        )
        self._presence_sub = self.create_subscription(
            Presence,
            "/mission/presence",
            self._on_presence,
            QOS_MISSION_PRESENCE,
            callback_group=self._cb_reentrant,
        )
        self._transcript_sub = self.create_subscription(
            Transcript,
            "/asr/transcript",
            self._on_transcript,
            QOS_ASR_TRANSCRIPT,
            callback_group=self._cb_reentrant,
        )
        self._wakeword_sub = self.create_subscription(
            Wakeword,
            "/speech/wakeword",
            self._on_wakeword,
            QOS_WAKEWORD,
            callback_group=self._cb_reentrant,
        )
        self._cancel_all_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_reentrant,
        )

        if self._raw_llm:
            self.get_logger().warning("llm.raw=true — чат без system/GBNF/инструментов")

        self.get_logger().info("dialog_agent сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Стянуть каталог локаций/туров, собрать системный промпт и ретривер, разрешить ходы."""
        del state
        try:
            return self._activate()
        except Exception as error:
            self.get_logger().error(f"activate не удался: {error}")
            return TransitionCallbackReturn.FAILURE

    def _activate(self) -> TransitionCallbackReturn:
        # Каталог -- ОДИН раз здесь, не на on_configure: tool_broker к
        # моменту конфигурации dialog_agent может быть ещё не активен.
        # Отсутствие каталога -- не мягкая деградация: агент без каталога
        # не может назвать ни одной локации, лучше не подняться, чем молча
        # работать вслепую (DIALOG_REWORK_PLAN.md §6.2).
        locations_result = self._execute_tool(
            "list_locations", {}, timeout_s=self._catalog_ns_timeout_s
        )
        if not locations_result.ok:
            self.get_logger().error(f"каталог локаций не пришёл: {locations_result.message}")
            return TransitionCallbackReturn.FAILURE
        tours_result = self._execute_tool("list_tours", {}, timeout_s=self._catalog_ns_timeout_s)
        if not tours_result.ok:
            self.get_logger().error(f"каталог туров не пришёл: {tours_result.message}")
            return TransitionCallbackReturn.FAILURE

        self._locations_catalog = list(locations_result.data.get("locations", []))
        self._tours_catalog = list(tours_result.data.get("tours", []))
        self._location_name_by_id = {
            loc["id"]: (loc["aliases"][0] if loc.get("aliases") else loc["id"])
            for loc in self._locations_catalog
        }
        self._location_zone_by_id = {
            loc["id"]: str(loc.get("zone", "")) for loc in self._locations_catalog
        }
        self._tour_name_by_id = {
            tour["id"]: tour.get("name", tour["id"]) for tour in self._tours_catalog
        }

        # Каталог инструментов НЕ идёт в системный промпт (CLAUDE_CODE_TASK.md
        # п.2) -- реплика иначе зачитывала вслух описания инструментов. Он
        # живёт в инструкции фазы действия; обе инструкции строятся один раз
        # здесь и обязаны быть побайтово одинаковыми на каждый ход (иначе
        # теряется CACHE_REUSE префикса).
        self._system_prompt = build_system_prompt(
            self._preamble,
            locations=self._locations_catalog,
            tours=self._tours_catalog,
        )
        self._action_instruction = build_action_instruction(schema.TOOLS)
        self._answer_instruction = build_answer_instruction()
        self._read_only_tool_names = frozenset(
            spec.name for spec in schema.TOOLS if spec.read_only
        )

        self._active = True
        return TransitionCallbackReturn.SUCCESS

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Запретить новые ходы (уже начатый -- доигрывает или получит abort снаружи)."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить кэш состояния/историю между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        with self._turn_lock:
            self._pending_text = None
        with self._state_lock:
            self._last_mission_state = None
            self._last_presence = None
            if hasattr(self, "_history"):
                self._history.clear()
            if hasattr(self, "_told_ids"):
                self._told_ids.clear()
            self._listen_until = 0.0
            self._wake_grace_until = 0.0
        # ROS-сущности уничтожаем явно: cleanup -> configure иначе оставляет
        # ВТОРОЙ комплект живых подписок/паблишеров (живой баг: задвоенные
        # записи в interaction_log). Идемпотентно -- teardown зовётся и из
        # on_cleanup, и из on_shutdown.
        for attr in (
            "_transcript_sub",
            "_wakeword_sub",
            "_mission_state_sub",
            "_presence_sub",
            "_cancel_all_sub",
        ):
            sub = getattr(self, attr, None)
            if sub is not None:
                self.destroy_subscription(sub)
                setattr(self, attr, None)
        client = getattr(self, "_call_tool_client", None)
        if client is not None:
            self.destroy_client(client)
            self._call_tool_client = None
        pub = getattr(self, "_interaction_pub", None)
        if pub is not None:
            self.destroy_publisher(pub)
            self._interaction_pub = None

    # -- кэш /mission/state, /mission/presence (свой, не tool_broker'а) ------

    def _on_mission_state(self, msg: MissionState) -> None:
        with self._state_lock:
            old = self._last_mission_state
            self._last_mission_state = msg
            if old is None:
                return
            for event_text in self._diff_events(old, msg):
                self._history.add_event(event_text, ts=time.time())
            # Очистка по окончании тура УБРАНА (CLAUDE_CODE_TASK.md п.4): живой
            # баг -- тур остановлен, посетитель продолжает говорить про него,
            # а история уже стёрта. Очистка остаётся только по отсутствию
            # посетителя, см. `_maybe_clear_history_for_absence_locked`.

    def _diff_events(self, old: MissionState, new: MissionState) -> list[str]:
        """События мимо диалога -- порождаются ТОЛЬКО на изменение поля (без дребезга)."""
        events: list[str] = []
        if old.state != new.state:
            events.append(f"перешёл в состояние {_STATE_NAMES.get(new.state, 'UNKNOWN')}")
        if old.stop_id != new.stop_id and new.stop_id:
            name = self._location_name_by_id.get(new.stop_id, new.stop_id)
            if new.stop_total:
                position = f" ({new.stop_index + 1} из {new.stop_total})"
            else:
                position = ""
            events.append(f"подошёл к остановке «{name}»{position}")
            self._told_ids.add(new.stop_id)
        if old.interrupt == MissionState.IRQ_NONE and new.interrupt != MissionState.IRQ_NONE:
            events.append("посетитель перебил вопросом")
        elif old.interrupt != MissionState.IRQ_NONE and new.interrupt == MissionState.IRQ_NONE:
            events.append("вернулся к рассказу")
        if not old.tour_id and new.tour_id:
            tour_name = self._tour_name_by_id.get(new.tour_id, new.tour_id)
            events.append(f"начался тур «{tour_name}»")
        elif old.tour_id and not new.tour_id:
            events.append("тур завершён")
        return events

    def _on_presence(self, msg: Presence) -> None:
        with self._state_lock:
            self._last_presence = msg
            if not msg.present:
                # stage2 A5: посетитель ушёл -- грейс-период без wakeword
                # больше не имеет смысла (следующий транскрипт не от него).
                self._wake_grace_until = 0.0

    def last_mission_state(self) -> MissionState | None:
        """Последнее полученное `/mission/state`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_mission_state

    def last_presence(self) -> Presence | None:
        """Последнее полученное `/mission/presence`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_presence

    def _maybe_clear_history_for_absence_locked(self) -> bool:
        """Очистка по отсутствию посетителя (DIALOG_REWORK_PLAN.md §6.3, пункт 1).

        Вызывать ТОЛЬКО держа `self._state_lock` -- читает `self._last_presence`
        напрямую (не через `last_presence()`, который сам берёт тот же
        незапирающийся повторно lock).
        """
        presence = self._last_presence
        if presence is None or presence.present:
            return False
        if presence.seconds_since_evidence <= self._history_clear_after_absent_s:
            return False
        if len(self._history) == 0 and not self._told_ids:
            return False
        self._history.clear()
        self._told_ids.clear()
        self.get_logger().info("история очищена: посетитель отсутствует достаточно долго")
        return True

    # -- barge-in: abort хода в полёте, не более -----------------------------

    def _on_cancel_all(self, msg: CancelAll) -> None:
        if msg.reason != CancelAll.REASON_BARGE_IN:
            return
        with self._turn_lock:
            abort_event = self._abort_event
        if abort_event is not None:
            self.get_logger().info("barge-in получен -- прерываю текущий ход")
            abort_event.set()

    # -- ASR: свои вопросы, мимо fast-path'а tool_broker ----------------------

    def _on_transcript(self, msg: Transcript) -> None:
        if not msg.is_final or not self._active:
            return
        # Голое wake-слово ("робот") -- не реплика: содержания для хода нет,
        # а ведущее "робот, ..." срезается, чтобы ЛЛМ не принимала его за
        # обращение в третьем лице (живой баг: "робот стоп" как существительное).
        text = matching.strip_wake_word(msg.text)
        if not text:
            self._arm_listen()
            self.get_logger().info("транскрипт -- только wake-слово, ход не запускаю")
            return
        mission = self.last_mission_state()
        if mission is None:
            return
        if mission.state == MissionState.STATE_IDLE and not matching.idle_turn_allowed(
            msg.text, listen_armed=self._listen_armed() or self._wake_grace_active()
        ):
            self.get_logger().info(f"IDLE без wakeword, игнор: {text!r}")
            return
        self._handle_transcript(text)

    def _on_wakeword(self, msg: Wakeword) -> None:
        """Открыть окно слушания только на активацию, не на стоп и не на эхо TTS."""
        if msg.tts_active:
            return
        keyword = (msg.keyword or "").strip().lower()
        if keyword not in _ACTIVATION_KEYWORDS:
            return
        self._arm_listen()

    def _arm_listen(self) -> None:
        with self._state_lock:
            self._listen_until = time.monotonic() + _LISTEN_WINDOW_S

    def _disarm_listen(self) -> None:
        with self._state_lock:
            self._listen_until = 0.0

    def _arm_wake_grace(self) -> None:
        """Продлить окно без wakeword на `wake_grace_s` от конца хода (stage2 A5)."""
        with self._state_lock:
            self._wake_grace_until = time.monotonic() + self._wake_grace_s

    def _wake_grace_active(self) -> bool:
        with self._state_lock:
            return time.monotonic() < self._wake_grace_until

    def _listen_armed(self) -> bool:
        with self._state_lock:
            return time.monotonic() < self._listen_until

    def _handle_transcript(self, text: str, *, is_replay: bool = False) -> None:
        """Обработать реплику: гейты -> fast-path -> старт хода (или отложить).

        `is_replay=True` -- реплика пришла из слота `_pending_text` после
        окончания предыдущего хода: если к этому моменту уже стартовал ход по
        более свежей реплике, отложенная молча устаревает (новое намерение
        побеждает), а не прерывает его. Состояние миссии здесь читается
        ЗАНОВО -- за время предыдущего хода оно могло измениться.
        """
        utterance_ts = time.time()
        mission = self.last_mission_state()
        if mission is None:
            return

        with self._state_lock:
            history_cleared = self._maybe_clear_history_for_absence_locked()

        if not self._raw_llm and self._fast_path_handles(mission.state, text):
            event_text = self._fast_path_event_text(mission.state, text)
            with self._state_lock:
                self._history.add_visitor(text, ts=time.time())
                self._history.add_event(event_text, ts=time.time())
            self.get_logger().info(f"fast-path уже обработал ({text!r}), ЛЛМ не зовём")
            return

        with self._turn_lock:
            if self._turn_in_flight:
                if is_replay:
                    self.get_logger().info(
                        "отложенная реплика устарела -- уже идёт ход по более свежей"
                    )
                    return
                # Новая финальная реплика делает текущий ход устаревшим --
                # та же семантика, что barge-in: прерываем и запоминаем
                # реплику, ход по ней начнётся сразу после освобождения.
                self._pending_text = text
                if self._abort_event is not None:
                    self._abort_event.set()
                self.get_logger().info(
                    "ход в полёте -- текущий прерван, новая реплика отложена в слот"
                )
                return
            self._turn_in_flight = True
            self._abort_event = threading.Event()
            self._turn_counter += 1
            turn_id = self._turn_counter

        threading.Thread(
            target=self._run_turn,
            args=(turn_id, mission, text, history_cleared, utterance_ts),
            daemon=True,
        ).start()

    def _fast_path_handles(self, mission_state: int, text: str) -> bool:
        """Проверить, обработал ли транскрипт fast-path -- ЛЛМ звать не нужно.

        Для `AWAITING_CONFIRM`/`ANSWERING` -- та же проверка, что
        `tool_broker_node._on_transcript` гоняет для того же топика (оба узла
        подписаны на `/asr/transcript` независимо): уверенный матч там
        означает, что `tool_broker` уже выполнил реальное действие
        (submit_confirm/submit_answer), и звать ЛЛМ поверх уже принятого
        решения нельзя. `IDLE` -- другой случай: там нет никакого мнения
        `tool_broker`, которое можно было бы задвоить (пустая команда
        отмены в IDLE не отображается ни в одно действие, гейтить нечего) --
        суппрессия здесь чисто локальная, против хода к ЛЛМ, который не мог
        бы предложить ничего лучше `noop` и рисковал бы вместо этого
        нафантазировать ответ (живой баг: "робот стоп" в IDLE был принят за
        существительное).
        """
        if mission_state == MissionState.STATE_AWAITING_CONFIRM:
            return matching.match_confirm(text) is not None
        if mission_state == MissionState.STATE_ANSWERING:
            return matching.match_stop_phrase(text)
        if mission_state == MissionState.STATE_IDLE:
            return matching.match_idle_dismiss(text)
        return False

    def _fast_path_event_text(self, mission_state: int, text: str) -> str:
        """Записать, что именно распознал fast-path -- не что сделал `tool_broker`.

        Агент не наблюдает действий `tool_broker` (DIALOG_REWORK_PLAN.md §6.4) --
        пишем в историю то, что видели сами, без вымысла.
        """
        if mission_state == MissionState.STATE_AWAITING_CONFIRM:
            is_yes = matching.match_confirm(text)
            return f"ответ обработан напрямую: подтверждение — {'да' if is_yes else 'нет'}"
        if mission_state == MissionState.STATE_IDLE:
            return "ответ обработан напрямую: команда отмены без содержания, ничего не делаю"
        return "ответ обработан напрямую: стоп-слово"

    def _run_raw_chat(self, text, *, complete_answer, speak, abort_event) -> TurnResult:
        """Один запрос: голый ASR-текст, без system prompt и инструментов."""
        messages = [{"role": "user", "content": text}]
        try:
            completion = complete_answer(messages)
        except BackendAborted:
            raise
        except BackendError:
            return TurnResult(messages=messages, stopped_reason="answer_backend_error")
        self.get_logger().info(f"Gemma: {completion.text!r}")
        answer_text = sanitize_answer(completion.text, max_chars=self._answer_max_chars)
        say_ok = False
        if answer_text and not abort_event.is_set():
            say_ok = speak(answer_text).ok
        return TurnResult(
            messages=[*messages, {"role": "assistant", "content": completion.text}],
            answer_text=answer_text,
            answer_raw_text=completion.text,
            answer_finish_reason=completion.finish_reason,
            say_ok=say_ok,
            stopped_reason="ok",
        )

    def _run_turn(
        self,
        turn_id: int,
        mission: MissionState,
        text: str,
        history_cleared: bool,
        utterance_ts: float,
    ) -> None:
        with self._turn_lock:
            abort_event = self._abort_event
        turn_start = time.monotonic()
        stage_timings: list[dict] = []
        snap: dict = {"mission": {"state": "UNKNOWN"}}
        references: list[dict] = []
        corpus_texts: list[str] = []
        result: TurnResult | None = None
        degraded = False
        degrade_reason: str | None = None
        told_ids: list[str] = []
        try:
            presence = self.last_presence() or _EmptyPresence()
            tools_allowed = schema.allowed_tools(mission.state, llm_only=True)

            with self._state_lock:
                told_ids = sorted(self._told_ids)
                history_messages, trailing_events = self._history.render()

            location_name = self._location_name_by_id.get(mission.stop_id, "")
            location_zone = self._location_zone_by_id.get(mission.stop_id, "")

            snap = snapshot.build_snapshot(
                mission,
                presence,
                tools_allowed=tools_allowed,
                location_zone=location_zone,
                location_name=location_name,
                told_ids=told_ids,
            )
            status_line = snapshot.render_status_line(snap)

            # Автосправка ДО фазы действия (CLAUDE_CODE_TASK_stage1_knowledge.md
            # п.7.1): текущая остановка целиком (если есть) + поиск по
            # реплике, всегда. Пропускается в llm.raw -- там нет ни
            # системного промпта, ни user_content, справка некому смотреть.
            knowledge_block = "СПРАВКА: ничего не найдено."
            if not self._raw_llm:
                lookup_data: dict | None = None
                if mission.exhibit_id:
                    lookup_start = time.monotonic()
                    lookup_result = self._execute_tool(
                        "lookup_content", {"content_id": mission.exhibit_id, "mode": "full"}
                    )
                    stage_timings.append(
                        {
                            "stage": "tool_call",
                            "tool": "lookup_content",
                            "ms": (time.monotonic() - lookup_start) * 1000,
                        }
                    )
                    if lookup_result.ok:
                        lookup_data = lookup_result.data

                search_start = time.monotonic()
                search_result = self._execute_tool(
                    "search_content", {"query": text, "max_results": 5}
                )
                stage_timings.append(
                    {
                        "stage": "tool_call",
                        "tool": "search_content",
                        "ms": (time.monotonic() - search_start) * 1000,
                    }
                )
                search_hits = search_result.data.get("hits", []) if search_result.ok else []

                candidates: list[dict] = []
                stop_title = ""
                if lookup_data:
                    stop_title = str(lookup_data.get("title", ""))
                    candidates.extend(
                        _spravka_candidates_from_lookup(mission.exhibit_id, lookup_data)
                    )
                candidates.extend(_spravka_candidates_from_hits(search_hits, source="auto"))

                knowledge_block, references, corpus_texts = _build_knowledge_block(
                    stop_title, candidates
                )

            # Реплика посетителя -- ПОСЛЕДНЯЯ строка последнего сообщения,
            # той же формы, что реплики в истории (голый текст, не JSON):
            # снимок уезжает служебной строкой [состояние: ...], хвостовые
            # события истории вклеиваются сюда же, а не отдельным
            # user-сообщением -- иначе 8B-модель отвечает на предпоследнее
            # «нормальное» сообщение вместо текущей реплики (живой баг).
            # СПРАВКА -- волатильный хвост ПОСЛЕ статус-строки: префикс до
            # неё (system + история + сама статус-строка) не меняется от
            # факта поиска, префикс-кэш не страдает.
            user_content = "\n".join(
                [f"СОБЫТИЕ: {event}" for event in trailing_events]
                + [status_line, knowledge_block, text]
            )
            self.get_logger().info(f"ASR: {text!r}")

            def _complete_answer(messages: list[dict]):
                start = time.monotonic()
                try:
                    return complete_with_fallback(
                        self._backends,
                        messages,
                        grammar=None,
                        max_tokens=self._max_tokens_answer,
                        temperature=self._temperature_answer,
                        abort_event=abort_event,
                        max_attempts_per_backend=self._max_attempts_per_backend,
                        backoff_s=self._backoff_s,
                    )
                finally:
                    stage_timings.append(
                        {"stage": "llm_answer", "ms": (time.monotonic() - start) * 1000}
                    )

            def _complete_action(messages: list[dict], grammar: str):
                start = time.monotonic()
                try:
                    return complete_with_fallback(
                        self._backends,
                        messages,
                        grammar=grammar,
                        max_tokens=self._max_tokens_action,
                        temperature=self._temperature_action,
                        abort_event=abort_event,
                        max_attempts_per_backend=self._max_attempts_per_backend,
                        backoff_s=self._backoff_s,
                    )
                finally:
                    stage_timings.append(
                        {"stage": "llm_action", "ms": (time.monotonic() - start) * 1000}
                    )

            def _speak(spoken_text: str) -> _RemoteToolResult:
                start = time.monotonic()
                try:
                    return self._execute_tool("say", {"text": spoken_text})
                finally:
                    stage_timings.append({"stage": "say", "ms": (time.monotonic() - start) * 1000})

            def _execute_tool_timed(name: str, args: dict) -> _RemoteToolResult:
                start = time.monotonic()
                try:
                    return self._execute_tool(name, args)
                finally:
                    stage_timings.append(
                        {
                            "stage": "tool_call",
                            "tool": name,
                            "ms": (time.monotonic() - start) * 1000,
                        }
                    )

            if self._raw_llm:
                result = self._run_raw_chat(
                    text, complete_answer=_complete_answer, speak=_speak, abort_event=abort_event
                )
            else:
                result = run_turn(
                    system_prompt=self._system_prompt,
                    history_messages=history_messages,
                    user_content=user_content,
                    complete_answer=_complete_answer,
                    complete_action=_complete_action,
                    speak=_speak,
                    execute_tool=_execute_tool_timed,
                    tool_names=tools_allowed,
                    action_instruction=self._action_instruction,
                    answer_instruction=self._answer_instruction,
                    repair_attempts=self._action_repair_attempts,
                    check_aborted=abort_event.is_set,
                    answer_max_chars=self._answer_max_chars,
                    read_only_tools=self._read_only_tool_names,
                )
            if (
                result.action is not None
                and result.action.read_only
                and result.action.result_ok
            ):
                # Явный read_only-вызов модели (фаза 1) -- те же чанки, что
                # видела фаза реплики, идут в references/corpus_texts с
                # source="tool", отдельно от source="auto" выше (п.7.3).
                tool_refs, tool_texts = _references_from_tool_result(result.action)
                references = [*references, *tool_refs]
                corpus_texts = [*corpus_texts, *tool_texts]
            degraded = result.stopped_reason in _DEGRADED_REASONS
            degrade_reason = result.stopped_reason if degraded else None
            action_name = result.action.name if result.action is not None else None
            self.get_logger().info(
                f"ход завершён: stopped_reason={result.stopped_reason} "
                f"say_ok={result.say_ok} action={action_name}"
            )
        except BackendAborted:
            self.get_logger().info("ход прерван barge-in -- частичный ответ отброшен")
            degraded = True
            degrade_reason = "aborted"
            result = TurnResult(stopped_reason="aborted")
        except BackendError as error:
            self.get_logger().warning(f"бэкенд недоступен: {error}")
            degraded = True
            degrade_reason = "backend_error"
            result = TurnResult(stopped_reason="backend_error")
        finally:
            with self._turn_lock:
                pending_text = self._pending_text
                self._pending_text = None
                self._turn_in_flight = False
                self._abort_event = None
            if pending_text is None:
                self._disarm_listen()
            self._arm_wake_grace()

        if result is not None:
            now = time.time()
            with self._state_lock:
                self._history.add_visitor(text, ts=now)
                truncated = abort_event.is_set() if abort_event is not None else False
                # Пустую или несказанную реплику в историю не пишем (живой
                # мусор: аборт хода оставлял пустое assistant-сообщение);
                # оборванная НА озвучке реплика остаётся с пометкой truncated.
                if result.answer_text and result.say_ok:
                    self._history.add_robot(result.answer_text, ts=now, truncated=truncated)
                action_event = _action_event_text(result.action)
                if action_event is not None:
                    self._history.add_event(action_event, ts=now)
                history_entries_after = len(self._history)

            record = build_interaction_record(
                turn_id=turn_id,
                session_id=self._session_id,
                utterance_ts=utterance_ts,
                mission_state_name=snap.get("mission", {}).get("state", "UNKNOWN"),
                utterance=text,
                snapshot=snap,
                references=references,
                corpus_texts=corpus_texts,
                result=result,
                stage_timings=stage_timings,
                history_entries=history_entries_after,
                history_cleared=history_cleared,
                told_ids=told_ids,
                degraded=degraded,
                degrade_reason=degrade_reason,
                total_ms=(time.monotonic() - turn_start) * 1000,
                now_s=now,
            )
            interaction_pub = getattr(self, "_interaction_pub", None)
            if interaction_pub is not None:
                try:
                    interaction_pub.publish(InteractionEvent(payload_json=json.dumps(record)))
                except Exception as error:  # noqa: BLE001 -- InvalidHandle на гонке teardown
                    # Демон-поток может пережить on_cleanup/on_shutdown --
                    # паблишер уже уничтожен, запись хода просто теряется.
                    self.get_logger().debug(f"публикация записи хода не удалась: {error}")

        # Реплей ПОСЛЕ коммита истории: новый ход обязан увидеть предыдущий
        # (в т.ч. прерванный) ход в истории. Если за эти миллисекунды успел
        # стартовать ход по ещё более свежей реплике -- is_replay молча
        # уступает ему.
        if pending_text is not None:
            self.get_logger().info("стартую отложенную реплику из слота")
            self._handle_transcript(pending_text, is_replay=True)

    def _execute_tool(
        self, name: str, args: dict, *, timeout_s: float | None = None
    ) -> _RemoteToolResult:
        timeout = timeout_s if timeout_s is not None else self._service_call_timeout_s
        client = getattr(self, "_call_tool_client", None)
        if client is None:
            return _RemoteToolResult(ok=False, message="узел уже разобран", data={})
        try:
            if not client.wait_for_service(timeout_sec=timeout):
                return _RemoteToolResult(ok=False, message="tool_broker недоступен", data={})
            request = CallTool.Request(name=name, args_json=json.dumps(args))
            future = client.call_async(request)
        except Exception as error:  # noqa: BLE001 -- InvalidHandle на гонке teardown
            # Демон-поток хода может пережить on_cleanup/on_shutdown: клиент
            # уничтожен teardown-ом, обращение к нему -- InvalidHandle. Это
            # штатная гонка завершения, не повод ронять поток с трейсбеком.
            return _RemoteToolResult(ok=False, message=f"клиент недоступен: {error}", data={})
        if not _wait_future(future, self.context, timeout):
            return _RemoteToolResult(ok=False, message="tool_broker не ответил вовремя", data={})
        response = future.result()
        try:
            data = json.loads(response.data_json) if response.data_json else {}
        except json.JSONDecodeError:
            data = {}
        return _RemoteToolResult(ok=response.ok, message=response.message, data=data)


def _action_event_text(action: ToolCallRecord | None) -> str | None:
    """Собрать итог действия как событие истории.

    `noop` НЕ порождает событие -- "ничего не делать" не несёт информации,
    которую стоило бы занести в историю. read_only-инструменты -- КОРОТКОЕ
    событие (`уточнил справку: <title>`, без текста), иначе история
    раздувается найденными фактами на каждый вопрос
    (CLAUDE_CODE_TASK_stage1_knowledge.md п.7.2). Для остальных (мутирующих)
    инструментов строка делегируется `turn.render_action_outcome` --
    истории и промпту фазы реплики положено видеть побайтово одинаковый
    итог.
    """
    if action is None or action.name == "noop":
        return None
    if action.read_only:
        return f"уточнил справку: {_read_only_result_title(action)}"
    return render_action_outcome(action)


def _read_only_result_title(action: ToolCallRecord) -> str:
    """Короткая подпись найденного read_only-результата -- для события истории."""
    data = action.result_data
    if data.get("title"):
        return str(data["title"])
    hits = data.get("hits") or []
    if hits:
        return str(hits[0].get("title", ""))
    candidates = data.get("candidates") or []
    if candidates:
        return str(candidates[0].get("id", ""))
    return str(action.args.get("content_id") or action.args.get("query") or "")


def _spravka_candidates_from_lookup(exhibit_id: str, lookup_data: dict) -> list[dict]:
    """Чанки текущей остановки (`lookup_content`) -- группа "stop", приоритет над находками."""
    chunks = lookup_data.get("chunks", [])
    chunk_ids = lookup_data.get("chunk_ids", [])
    return [
        {
            "text": text,
            "group": "stop",
            "ref": {
                "content_id": exhibit_id,
                "chunk_id": chunk_id,
                "score": 0.0,
                "source": "auto",
            },
        }
        for text, chunk_id in zip(chunks, chunk_ids, strict=False)
    ]


def _spravka_candidates_from_hits(hits: list[dict], *, source: str) -> list[dict]:
    """Находки `search_content` -- группа "hit", уже отсортированы content_server-ом по score."""
    return [
        {
            "text": hit.get("text", ""),
            "group": "hit",
            "kind": hit.get("kind", ""),
            "title": hit.get("title", ""),
            "ref": {
                "content_id": hit.get("content_id", ""),
                "chunk_id": hit.get("chunk_id", ""),
                "score": hit.get("score", 0.0),
                "source": source,
            },
        }
        for hit in hits
    ]


_SPRAVKA_CHAR_BUDGET = 2500


def _build_knowledge_block(
    stop_title: str, candidates: list[dict]
) -> tuple[str, list[dict], list[str]]:
    """СПРАВКА-блок для `user_content` + `references` + тексты для verbatim.

    `candidates` уже в приоритетном порядке: чанки текущей остановки первыми
    (группа "stop"), затем находки `search_content` по убыванию score
    (группа "hit") -- CLAUDE_CODE_TASK_stage1_knowledge.md п.7.1/7.4.
    Обрезка -- по целым чанкам суммарно на ≤2500 символов; первый чанк
    входит всегда, даже если сам длиннее бюджета (иначе резать нечего).
    Пустой результат -- строка "СПРАВКА: ничего не найдено." (модель видит,
    что искали, а не молчание), без записей в `references`.
    """
    kept: list[dict] = []
    used_chars = 0
    for candidate in candidates:
        length = len(candidate["text"])
        if kept and used_chars + length > _SPRAVKA_CHAR_BUDGET:
            break
        kept.append(candidate)
        used_chars += length

    if not kept:
        return "СПРАВКА: ничего не найдено.", [], []

    lines = ["СПРАВКА (только эти факты, своими словами):"]
    stop_texts = [c["text"] for c in kept if c["group"] == "stop"]
    if stop_texts:
        lines.append(f"[текущая остановка: {stop_title}] " + " ".join(stop_texts))
    for candidate in kept:
        if candidate["group"] != "hit":
            continue
        lines.append(f"[{candidate['kind']}: {candidate['title']}] {candidate['text']}")

    references = [candidate["ref"] for candidate in kept]
    corpus_texts = [candidate["text"] for candidate in kept]
    return "\n".join(lines), references, corpus_texts


def _references_from_tool_result(action: ToolCallRecord) -> tuple[list[dict], list[str]]:
    """references+тексты чанков, которые модель увидела через ЯВНЫЙ read_only-вызов.

    `source: "tool"` -- отличает их от автосправки (`source: "auto"`) в
    одном и том же логе (CLAUDE_CODE_TASK_stage1_knowledge.md п.7.3).
    `resolve_location` сюда не попадает -- отдаёт локации, не текст чанков,
    цитировать нечего.
    """
    data = action.result_data
    if action.name == "lookup_content":
        content_id = str(action.args.get("content_id", ""))
        chunks = data.get("chunks", [])
        chunk_ids = data.get("chunk_ids", [])
        refs = [
            {"content_id": content_id, "chunk_id": chunk_id, "score": 0.0, "source": "tool"}
            for chunk_id in chunk_ids
        ]
        return refs, list(chunks)
    if action.name == "search_content":
        hits = data.get("hits", [])
        refs = [
            {
                "content_id": hit.get("content_id", ""),
                "chunk_id": hit.get("chunk_id", ""),
                "score": hit.get("score", 0.0),
                "source": "tool",
            }
            for hit in hits
        ]
        return refs, [hit.get("text", "") for hit in hits]
    return [], []


def main(args: list[str] | None = None) -> None:
    """Точка входа."""
    rclpy.init(args=args)
    node = DialogAgentNode()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
