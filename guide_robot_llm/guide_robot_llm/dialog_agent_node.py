"""dialog_agent -- ReAct-цикл поверх llm_client + tool_broker (llm_plam.md §3/§5/§6).

Отдельный процесс от `tool_broker` (см. `tool_broker_node.py:main()` --
`rclpy.init` -> один узел -> `spin()`, `llm.launch.py` запускает его своим
`Node(executable=...)`), поэтому `call_tool()` -- голый Python-метод --
недостижим напрямую: зовём через `~/call_tool` сервис
(`guide_robot_msgs/srv/CallTool.srv`), который `tool_broker_node.py` теперь
предоставляет.

Кэш `/mission/state`/`/mission/presence` -- свой, отдельный от `tool_broker`
(разные процессы, разные подписки на один и тот же топик). ReAct-шаг сам по
себе -- чистая логика в `dialog/loop.py`, эта нода только собирает вход
(снимок + реплика), инжектирует `complete`/`execute_tool` и реагирует на
ROS-события (транскрипт, barge-in).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.lifecycle import LifecycleNode, State, TransitionCallbackReturn
from rclpy.task import Future

from guide_robot_llm import matching, snapshot
from guide_robot_llm.dialog.interaction_log import build_interaction_record
from guide_robot_llm.dialog.loop import ReactTurnResult, run_react_turn
from guide_robot_llm.dialog.prompt import build_system_prompt
from guide_robot_llm.lib.qos import (
    QOS_ASR_TRANSCRIPT,
    QOS_CANCEL_ALL,
    QOS_INTERACTION_EVENT,
    QOS_MISSION_PRESENCE,
    QOS_MISSION_STATE,
)
from guide_robot_llm.llm_client import Backend, BackendConfig, complete_with_fallback
from guide_robot_llm.llm_client.errors import BackendAborted, BackendError
from guide_robot_llm.tools import schema
from guide_robot_msgs.msg import CancelAll, InteractionEvent, MissionState, Presence, Transcript
from guide_robot_msgs.srv import CallTool

__all__ = ["DialogAgentNode", "main"]


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
    """Ответ `~/call_tool`, приведённый к форме `ToolResultLike` для `dialog/loop.py`."""

    ok: bool
    message: str
    data: dict


@dataclass
class _EmptyPresence:
    """Заглушка, пока /mission/presence ещё не пришёл ни разу."""

    present: bool = False
    seconds_since_evidence: float = 0.0


class DialogAgentNode(LifecycleNode):
    """Lifecycle-нода: ReAct-цикл, слушает ASR/mission/cancel_all, зовёт tool_broker по сети."""

    def __init__(self, **node_kwargs: object) -> None:
        """Объявить параметры. Бэкенды/ROS-ресурсы -- в `on_configure`."""
        super().__init__("dialog_agent", **node_kwargs)

        self.declare_parameter("llm.base_urls", ["http://127.0.0.1:18080/v1"])
        self.declare_parameter("llm.connect_timeout_s", 2.0)
        self.declare_parameter("llm.read_timeout_s", 30.0)
        self.declare_parameter("llm.max_tokens", 512)
        self.declare_parameter("llm.temperature", 0.2)
        self.declare_parameter("llm.max_attempts_per_backend", 2)
        self.declare_parameter("llm.backoff_s", 0.5)
        self.declare_parameter("llm.api_key", "")
        self.declare_parameter("system_prompt_path", "")
        self.declare_parameter("tool_broker_ns", "/tool_broker")
        self.declare_parameter("service_call_timeout_s", 2.0)
        self.declare_parameter("max_tool_calls_per_turn", 2)

        self._active = False
        self._state_lock = threading.Lock()
        self._last_mission_state: MissionState | None = None
        self._last_presence: Presence | None = None

        self._turn_lock = threading.Lock()
        self._turn_in_flight = False
        self._abort_event: threading.Event | None = None
        self._turn_counter = 0

        self._cb_reentrant = ReentrantCallbackGroup()

    # -- lifecycle ------------------------------------------------------

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Прочитать параметры, поднять бэкенды/клиента/подписки."""
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
        self._max_tokens = int(self.get_parameter("llm.max_tokens").value)
        self._temperature = float(self.get_parameter("llm.temperature").value)
        self._max_attempts_per_backend = int(
            self.get_parameter("llm.max_attempts_per_backend").value
        )
        self._backoff_s = float(self.get_parameter("llm.backoff_s").value)
        self._max_tool_calls_per_turn = int(self.get_parameter("max_tool_calls_per_turn").value)
        self._service_call_timeout_s = float(self.get_parameter("service_call_timeout_s").value)
        tool_broker_ns = str(self.get_parameter("tool_broker_ns").value)

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

        # Преамбул -- из файла (см. dialog/prompt.py: та же копия должна
        # греть llm_server/config/system_prompt.txt), каталог инструментов --
        # ВЕСЬ tools.schema.TOOLS, не отфильтрованный по текущему mission
        # state: системный промпт обязан быть побайтово одинаков каждый ход
        # (CACHE_REUSE), гейт по состоянию идёт через tools_allowed в
        # снимке (волатильная часть), не через состав системного промпта.
        system_prompt_path = str(self.get_parameter("system_prompt_path").value)
        preamble = Path(system_prompt_path).read_text(encoding="utf-8")
        self._system_prompt = build_system_prompt(preamble, schema.TOOLS)

        self._call_tool_client = self.create_client(
            CallTool, f"{tool_broker_ns}/call_tool", callback_group=self._cb_reentrant
        )
        # fire-and-forget, llm_plam.md §6: interaction_log может быть не
        # запущен -- ход диалога не обязан на него оглядываться (симметрично
        # тому, что tool_broker остаётся рабочим без dialog_agent).
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
        self._cancel_all_sub = self.create_subscription(
            CancelAll,
            "/speech/cancel_all",
            self._on_cancel_all,
            QOS_CANCEL_ALL,
            callback_group=self._cb_reentrant,
        )

        self.get_logger().info("dialog_agent сконфигурирован")
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Разрешить обработку транскриптов."""
        self._active = True
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Запретить новые ходы (уже начатый -- доигрывает или получит abort снаружи)."""
        self._active = False
        return super().on_deactivate(state)

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Сбросить кэш состояния между сессиями конфигурации."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Как cleanup."""
        del state
        self._teardown()
        return TransitionCallbackReturn.SUCCESS

    def _teardown(self) -> None:
        with self._state_lock:
            self._last_mission_state = None
            self._last_presence = None

    # -- кэш /mission/state, /mission/presence (свой, не tool_broker'а) ------

    def _on_mission_state(self, msg: MissionState) -> None:
        with self._state_lock:
            self._last_mission_state = msg

    def _on_presence(self, msg: Presence) -> None:
        with self._state_lock:
            self._last_presence = msg

    def last_mission_state(self) -> MissionState | None:
        """Последнее полученное `/mission/state`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_mission_state

    def last_presence(self) -> Presence | None:
        """Последнее полученное `/mission/presence`, либо `None`, если ещё не пришло."""
        with self._state_lock:
            return self._last_presence

    # -- barge-in: abort хода в полёте, не более (llm_plam.md §6) ------------

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
        mission = self.last_mission_state()
        if mission is None:
            return

        if self._fast_path_handles(mission.state, msg.text):
            self.get_logger().info(f"fast-path уже обработал ({msg.text!r}), ЛЛМ не зовём")
            return

        with self._turn_lock:
            if self._turn_in_flight:
                self.get_logger().info("ход уже в полёте -- новый транскрипт пропущен")
                return
            self._turn_in_flight = True
            self._abort_event = threading.Event()
            self._turn_counter += 1
            turn_id = self._turn_counter

        threading.Thread(
            target=self._run_turn, args=(turn_id, mission, msg.text), daemon=True
        ).start()

    def _fast_path_handles(self, mission_state: int, text: str) -> bool:
        """Проверить, обработал ли `tool_broker` уже (сам, независимо) этот транскрипт.

        Та же проверка, что `tool_broker_node._on_transcript` гоняет для
        того же топика -- оба узла подписаны на `/asr/transcript`
        независимо (llm_plam.md §9: "matching.py разбирает... локально...
        ЛЛМ подключается только при непонятном ответе"). Уверенный матч
        здесь означает, что звать ЛЛМ поверх уже принятого решения нельзя --
        второй, потенциально противоречащий tool-call на тот же ход.
        """
        if mission_state == MissionState.STATE_AWAITING_CONFIRM:
            return matching.match_confirm(text) is not None
        if mission_state == MissionState.STATE_ANSWERING:
            return matching.match_stop_phrase(text)
        return False

    def _run_turn(self, turn_id: int, mission: MissionState, text: str) -> None:
        with self._turn_lock:
            abort_event = self._abort_event
        turn_start = time.monotonic()
        stage_timings: list[dict] = []
        snap: dict = {"mission": {"state": "UNKNOWN"}}
        result: ReactTurnResult | None = None
        degraded = False
        degrade_reason: str | None = None
        try:
            presence = self.last_presence() or _EmptyPresence()
            tools_allowed = schema.allowed_tools(mission.state)
            snap = snapshot.build_snapshot(mission, presence, tools_allowed=tools_allowed)
            user_content = json.dumps({"snapshot": snap, "utterance": text})

            def _complete(messages: list[dict], grammar: str):
                start = time.monotonic()
                try:
                    return complete_with_fallback(
                        self._backends,
                        messages,
                        grammar=grammar,
                        max_tokens=self._max_tokens,
                        temperature=self._temperature,
                        abort_event=abort_event,
                        max_attempts_per_backend=self._max_attempts_per_backend,
                        backoff_s=self._backoff_s,
                    )
                finally:
                    stage_timings.append(
                        {
                            "stage": "llm_call",
                            "tool": None,
                            "ms": (time.monotonic() - start) * 1000,
                        }
                    )

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

            result = run_react_turn(
                system_prompt=self._system_prompt,
                user_content=user_content,
                complete=_complete,
                execute_tool=_execute_tool_timed,
                tool_names=tools_allowed,
                max_tool_calls=self._max_tool_calls_per_turn,
            )
            degraded = result.stopped_reason == "backend_error"
            degrade_reason = "backend_error" if degraded else None
            calls = [call.name for call in result.calls]
            self.get_logger().info(
                f"ход завершён: stopped_reason={result.stopped_reason} calls={calls}"
            )
        except BackendAborted:
            self.get_logger().info("ход прерван barge-in -- частичный ответ отброшен")
            degraded = True
            degrade_reason = "aborted"
            result = ReactTurnResult(messages=[], calls=[], stopped_reason="aborted")
        except BackendError as error:
            self.get_logger().warning(f"бэкенд недоступен: {error}")
            degraded = True
            degrade_reason = "backend_error"
            result = ReactTurnResult(messages=[], calls=[], stopped_reason="backend_error")
        finally:
            with self._turn_lock:
                self._turn_in_flight = False
                self._abort_event = None

        if result is not None:
            record = build_interaction_record(
                turn_id=turn_id,
                mission_state_name=snap.get("mission", {}).get("state", "UNKNOWN"),
                utterance=text,
                snapshot=snap,
                result=result,
                stage_timings=stage_timings,
                degraded=degraded,
                degrade_reason=degrade_reason,
                total_ms=(time.monotonic() - turn_start) * 1000,
                now_s=time.time(),
            )
            self._interaction_pub.publish(InteractionEvent(payload_json=json.dumps(record)))

    def _execute_tool(self, name: str, args: dict) -> _RemoteToolResult:
        if not self._call_tool_client.wait_for_service(  # type: ignore[attr-defined]
            timeout_sec=self._service_call_timeout_s
        ):
            return _RemoteToolResult(ok=False, message="tool_broker недоступен", data={})
        request = CallTool.Request(name=name, args_json=json.dumps(args))
        future = self._call_tool_client.call_async(request)  # type: ignore[attr-defined]
        if not _wait_future(future, self.context, self._service_call_timeout_s):
            return _RemoteToolResult(ok=False, message="tool_broker не ответил вовремя", data={})
        response = future.result()
        try:
            data = json.loads(response.data_json) if response.data_json else {}
        except json.JSONDecodeError:
            data = {}
        return _RemoteToolResult(ok=response.ok, message=response.message, data=data)


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
