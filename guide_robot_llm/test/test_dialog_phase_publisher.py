"""/dialog/phase на реальном стеке (CLAUDE_CODE_TASK_face_stage2.md §1.4).

Тот же харнесс, что `test_dialog_agent_e2e.py`: реальный `DialogAgentNode`
рядом с `tool_broker`/`mission_fsm`, `MockLlmServer` вместо `llm_server/`.
Здесь -- только последовательность фаз, наблюдаемая подписчиком на
`/dialog/phase` с тем же QoS, что у паблишера (иначе подписка молча не
подключится).
"""

from __future__ import annotations

import json
import threading
import time

from guide_robot_llm.lib.qos import QOS_CANCEL_ALL, QOS_DIALOG_PHASE, QOS_MISSION_PRESENCE

from guide_robot_msgs.msg import CancelAll, DialogPhase, Presence, Transcript
from test.mocks.harness import ToolBrokerTestHarness, wait_until
from test.mocks.mock_llm_server import MockLlmServer

_NOOP = json.dumps({"tool": "reply", "args": {}})
_PHASE_NAMES = {
    DialogPhase.IDLE: "IDLE",
    DialogPhase.ACTION: "ACTION",
    DialogPhase.ANSWER: "ANSWER",
    DialogPhase.AWAITING: "AWAITING",
}


class _PhaseRecorder:
    """Собирает последовательность опубликованных `phase` (имена, не числа)."""

    def __init__(self, harness: ToolBrokerTestHarness) -> None:
        self._lock = threading.Lock()
        self.phases: list[str] = []
        node = harness.make_client_node("phase_recorder")
        node.create_subscription(DialogPhase, "/dialog/phase", self._on_msg, QOS_DIALOG_PHASE)

    def _on_msg(self, msg: DialogPhase) -> None:
        with self._lock:
            self.phases.append(_PHASE_NAMES[msg.phase])

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.phases)


def _dialog_agent_has_mission_state(harness: ToolBrokerTestHarness):
    return lambda: harness.dialog_agent.last_mission_state() is not None


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def test_normal_turn_goes_action_answer_idle_never_dropping_to_idle_between() -> None:
    """ACTION -> ANSWER -> IDLE, без видимого IDLE между фазой 1 и фазой 2 (§3)."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.chunks_no_grammar = ["Привет!"]
        harness.llm_server.chunks_with_grammar = [_NOOP]

        client = harness.make_client_node()
        _publish_transcript(client, "робот, привет")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
        wait_until(lambda: recorder.snapshot()[-1:] == ["IDLE"], timeout_s=5.0)

        phases = recorder.snapshot()
        assert phases == ["ACTION", "ANSWER", "IDLE"], phases
    finally:
        harness.shutdown()


def test_ask_visitor_turn_reaches_awaiting_only_after_answer_phase_returns() -> None:
    """thinking(ACTION+ANSWER) -> AWAITING, не ACTION -> AWAITING напрямую (§1.4 corrigendum)."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.chunks_no_grammar = ["Прервать экскурсию и пойти к лидару?"]
        harness.llm_server.chunks_with_grammar = [
            json.dumps(
                {
                    "think": "нужно подтверждение перед движением",
                    "tool": "ask_visitor",
                    "args": {
                        "question": "Прервать экскурсию и пойти к лидару?",
                        "on_yes": {"tool": "reply", "args": {}},
                        "on_no": "Хорошо, продолжаем.",
                    },
                }
            )
        ]

        client = harness.make_client_node()
        _publish_transcript(client, "робот, хочу к лидару")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
        wait_until(lambda: "AWAITING" in recorder.snapshot(), timeout_s=5.0)

        phases = recorder.snapshot()
        assert phases == ["ACTION", "ANSWER", "AWAITING"], phases
    finally:
        harness.shutdown()


def test_ask_visitor_fast_path_yes_never_publishes_action() -> None:
    """«да» на слот живого ask_visitor не должно дать ACTION ни на один тик."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.chunks_no_grammar = ["Прервать экскурсию и пойти к лидару?"]
        harness.llm_server.chunks_with_grammar = [
            json.dumps(
                {
                    "think": "нужно подтверждение перед движением",
                    "tool": "ask_visitor",
                    "args": {
                        "question": "Прервать экскурсию и пойти к лидару?",
                        "on_yes": {"tool": "reply", "args": {}},
                        "on_no": "Хорошо, продолжаем.",
                    },
                }
            )
        ]

        client = harness.make_client_node()
        _publish_transcript(client, "робот, хочу к лидару")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
        wait_until(lambda: "AWAITING" in recorder.snapshot(), timeout_s=5.0)

        # Слот _pending_question живой -- «да» идёт fast-path'ом,
        # _run_pending_answer_phase, run_turn() (и потому ACTION) не звучит.
        # on_yes=reply всё равно доходит до фазы реплики (run_answer_phase),
        # которая зовёт ЛЛМ -- свежий chunks_no_grammar ей нужен.
        harness.llm_server.chunks_no_grammar = ["Хорошо."]
        _publish_transcript(client, "да")
        wait_until(lambda: harness.say.goals_received >= 2, timeout_s=5.0)
        wait_until(lambda: recorder.snapshot()[-1:] == ["IDLE"], timeout_s=5.0)

        phases = recorder.snapshot()
        assert phases == ["ACTION", "ANSWER", "AWAITING", "ANSWER", "IDLE"], phases
    finally:
        harness.shutdown()


def test_backend_error_on_action_phase_returns_to_idle() -> None:
    """Ход, упавший на фазе действия (BackendError), обязан вернуть IDLE, не залипать."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.mode = MockLlmServer.MODE_HTTP_ERROR

        client = harness.make_client_node()
        _publish_transcript(client, "робот, привет")

        wait_until(lambda: len(recorder.snapshot()) >= 2, timeout_s=10.0)

        phases = recorder.snapshot()
        assert phases == ["ACTION", "IDLE"], phases
        assert harness.say.goals_received == 0
    finally:
        harness.shutdown()


def test_barge_in_during_turn_returns_to_idle_not_stuck_thinking() -> None:
    """barge-in (BackendAborted) обязан вернуть IDLE, а не оставить лицо в thinking."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        harness.llm_server.chunks_with_grammar = [
            '{"think": "',
            "думаю",
            '"',
            ", ",
            '"tool": "reply", "args": {}}',
        ]
        harness.llm_server.chunks_no_grammar = ["ок"]
        harness.llm_server.chunk_delay_s = 0.3

        client = harness.make_client_node()
        _publish_transcript(client, "робот, расскажи что-нибудь длинное")
        wait_until(lambda: "ACTION" in recorder.snapshot(), timeout_s=5.0)
        time.sleep(0.15)  # дать ходу получить хотя бы первый чанк фазы действия

        cancel_pub = client.create_publisher(CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL)
        cancel_pub.publish(CancelAll(reason=CancelAll.REASON_BARGE_IN))

        wait_until(lambda: recorder.snapshot()[-1:] == ["IDLE"], timeout_s=10.0)
    finally:
        harness.shutdown()


def test_presence_change_alone_republishes_current_phase() -> None:
    """§2.8 -- (phase, presence) -- смена ЛЮБОЙ половины пары обязана republish'ить,
    не только смена phase (harness не поднимает presence_monitor -- шлём /mission/presence
    напрямую)."""
    harness = ToolBrokerTestHarness()
    recorder = _PhaseRecorder(harness)
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        time.sleep(0.2)  # дать бы лишнему сообщению появиться, если дедуп сломан
        assert recorder.snapshot() == []  # ни одного хода/presence-события ещё не было

        client = harness.make_client_node()
        presence_pub = client.create_publisher(
            Presence, "/mission/presence", QOS_MISSION_PRESENCE
        )
        presence_pub.publish(Presence(present=True))

        wait_until(lambda: len(recorder.snapshot()) >= 1, timeout_s=5.0)
        phases = recorder.snapshot()
        # phase не менялся (остался в дефолтном IDLE) -- republish только
        # потому, что presence сменился False -> True.
        assert phases == ["IDLE"], phases
    finally:
        harness.shutdown()
