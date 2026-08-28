"""dialog_agent end-to-end на реальном mission-стеке + MockLlmServer (DIALOG_REWORK_PLAN.md).

`ToolBrokerTestHarness` поднимает `DialogAgentNode` рядом с
`tool_broker`/`mission_fsm`/`narration_server`, направленным на
`MockLlmServer` вместо настоящего `llm_server/`. Проверяется путь целиком:
транскрипт -> ход (фаза действия: tool-call -> `~/call_tool`; фаза реплики:
текст -> `speak()`) -> реальный `tool_broker` -> реальный mission-стек ->
мок голоса/навигации.

`MockLlmServer.chunks_no_grammar`/`chunks_with_grammar` различают фазы: фаза
действия идёт с GBNF в теле запроса, фаза реплики -- без (см. `Backend.complete()`).
"""

from __future__ import annotations

import json
import time

from guide_robot_llm.lib.qos import QOS_MISSION_STATE

from guide_robot_msgs.msg import CancelAll, MissionState, Transcript
from test.mocks.harness import ToolBrokerTestHarness, pump_clock, wait_until
from test.mocks.mock_llm_server import MockLlmServer

_S = MissionState
_NOOP = json.dumps({"tool": "noop", "args": {}})


def _mission_state_is(harness: ToolBrokerTestHarness, target: int):
    def _predicate() -> bool:
        state = harness.broker.last_mission_state()
        return state is not None and state.state == target

    return _predicate


def _dialog_agent_has_mission_state(harness: ToolBrokerTestHarness):
    return lambda: harness.dialog_agent.last_mission_state() is not None


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def _log_lines(harness: ToolBrokerTestHarness) -> list[dict]:
    path = harness.interaction_log._sink.path  # noqa: SLF001 -- тестовая интроспекция
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _setup_two_stop_tour(harness: ToolBrokerTestHarness) -> None:
    for i, stop_id in enumerate(("stop0", "stop1")):
        harness.fixtures.add_exhibit(stop_id, [f"{stop_id} ч0.", f"{stop_id} ч1."], version="r1")
        harness.fixtures.add_location(stop_id, x=float(i), y=0.0)
    harness.nav.duration_s = 0.05
    harness.say.chars_per_sec = 50.0


def test_transcript_in_idle_drives_say_through_call_tool() -> None:
    """Фаза 1 отвечает текстом -> `speak()` зовёт `call_tool("say", ...)` -> MockSayServer."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.chunks_no_grammar = ["Привет!"]
        harness.llm_server.chunks_with_grammar = [_NOOP]

        client = harness.make_client_node()
        _publish_transcript(client, "робот, привет")

        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)

        # Формат последнего сообщения хода: голый текст со служебной строкой
        # [состояние: ...], реплика -- последней строкой, никакого JSON-снимка.
        body = harness.llm_server.last_request_body
        assert body is not None
        finals = [
            m["content"]
            for m in body["messages"]
            if m["role"] == "user" and m["content"].endswith("привет")
        ]
        assert finals, "сообщение с текущей репликой не найдено в запросе к ЛЛМ"
        assert "[состояние: IDLE" in finals[0]
        assert '"snapshot"' not in finals[0] and '"utterance"' not in finals[0]
    finally:
        harness.shutdown()


def test_search_content_hit_appears_in_spravka_and_references() -> None:
    """CLAUDE_CODE_TASK_stage1_knowledge.md п.7.1/7.3: автосправка + references в логе."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.fixtures.set_search_hits(
            [
                {
                    "content_id": "livox_mid70",
                    "kind": "exhibit",
                    "title": "Лидар",
                    "chunk_id": "c1",
                    "text": "Это твердотельный лидар Livox Mid-70.",
                    "score": 1.5,
                    "version": "v1",
                }
            ]
        )
        harness.llm_server.chunks_no_grammar = ["Это лидар."]
        harness.llm_server.chunks_with_grammar = [_NOOP]

        client = harness.make_client_node()
        _publish_transcript(client, "робот, что это за штука")

        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)

        body = harness.llm_server.last_request_body
        assert body is not None
        finals = [
            m["content"]
            for m in body["messages"]
            if m["role"] == "user" and m["content"].endswith("что это за штука")
        ]
        assert finals, "сообщение с текущей репликой не найдено в запросе к ЛЛМ"
        assert "СПРАВКА (только эти факты" in finals[0]
        assert "[exhibit: Лидар] Это твердотельный лидар Livox Mid-70." in finals[0]

        wait_until(lambda: len(_log_lines(harness)) >= 1, timeout_s=5.0)
        record = _log_lines(harness)[0]
        assert record["references"] == [
            {"content_id": "livox_mid70", "chunk_id": "c1", "score": 1.5, "source": "auto"}
        ]
    finally:
        harness.shutdown()


def test_bare_wake_word_does_not_start_a_turn() -> None:
    """Голое "робот" (wake-слово без содержания) не должно порождать ход к ЛЛМ."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)

        client = harness.make_client_node()
        _publish_transcript(client, "робот")

        time.sleep(0.3)  # дать бы ходу стартовать, если фильтр не сработал
        assert harness.llm_server.last_request_body is None, "ЛЛМ не должен был вызываться вовсе"
        assert harness.say.goals_received == 0
    finally:
        harness.shutdown()


def test_idle_chit_chat_without_wakeword_does_not_start_a_turn() -> None:
    """IDLE без «робот» -- не диалог (мусор ASR не должен порождать ход)."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        client = harness.make_client_node()
        _publish_transcript(client, "привет")
        time.sleep(0.3)
        assert harness.llm_server.last_request_body is None, "ЛЛМ не должен был вызываться вовсе"
        assert harness.say.goals_received == 0
    finally:
        harness.shutdown()


def test_action_reaches_tool_broker_then_answer_is_spoken() -> None:
    """Фаза действия выбирает guide_to -- оно доходит до tool_broker/mission_fsm
    ДО реплики (порядок фаз инвертирован), а реплика следует после."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.fixtures.add_exhibit("lab105a", ["Раз.", "Два."], version="rev1")
        harness.fixtures.add_location("lab105a", x=1.0, y=2.0)
        harness.nav.duration_s = 0.05
        harness.say.chars_per_sec = 50.0

        harness.llm_server.chunks_no_grammar = ["Идём в лабораторию."]
        harness.llm_server.chunks_with_grammar = [
            json.dumps(
                {
                    "think": "посетитель просит отвести в лабораторию",
                    "tool": "guide_to",
                    "args": {"location_id": "lab105a"},
                }
            )
        ]

        client = harness.make_client_node()
        _publish_transcript(client, "отведи меня в лабораторию")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
    finally:
        harness.shutdown()


def test_start_tour_from_dialog_sends_greet_false_and_skips_greeting_state() -> None:
    """stage2 A2: реплика фазы 2 по итогу start_tour и есть приветствие -- заготовленный
    Say из GreetingState иначе звучит дублем следом. RunTour.Goal.greet=False, тур
    стартует прямо в NAVIGATING, GREETING в последовательности состояний не появляется."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.fixtures.add_exhibit("stop0", ["Раз."], version="rev1")
        harness.fixtures.add_location("stop0", x=1.0, y=0.0)
        harness.fixtures.add_tour("full", "Полный тур", [("stop0", "stop0", 0, "short")])
        harness.nav.duration_s = 0.05
        harness.say.chars_per_sec = 50.0

        states_seen: list[int] = []
        client = harness.make_client_node()
        client.create_subscription(
            MissionState,
            "/mission/state",
            lambda msg: states_seen.append(msg.state),
            QOS_MISSION_STATE,
        )

        harness.llm_server.chunks_no_grammar = ["Начинаем экскурсию."]
        harness.llm_server.chunks_with_grammar = [
            json.dumps(
                {
                    "think": "явная просьба начать тур",
                    "tool": "start_tour",
                    "args": {"tour_id": "full"},
                }
            )
        ]

        _publish_transcript(client, "проведи экскурсию")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
        assert _S.STATE_GREETING not in states_seen
    finally:
        harness.shutdown()


def test_barge_in_aborts_in_flight_turn_before_tool_executes() -> None:
    """DIALOG_REWORK_PLAN.md: abort реального HTTP-запроса -- speak() для оборванного хода
    не зовётся, а следующий ход после abort-а проходит штатно (агент разблокировался)."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        # Первая фаза хода теперь -- действие (с грамматикой): медленный стрим
        # именно её, чтобы barge-in пришёлся на генерацию в полёте.
        harness.llm_server.chunks_with_grammar = [
            '{"think": "',
            "думаю",
            '"',
            ", ",
            '"tool": "noop", "args": {}}',
        ]
        harness.llm_server.chunks_no_grammar = ["ок"]
        harness.llm_server.chunk_delay_s = 0.3

        client = harness.make_client_node()
        _publish_transcript(client, "робот, расскажи что-нибудь длинное")
        time.sleep(0.15)  # дать ходу начаться и получить хотя бы первый чанк фазы действия

        cancel_pub = client.create_publisher(CancelAll, "/speech/cancel_all", 1)
        cancel_pub.publish(CancelAll(reason=CancelAll.REASON_BARGE_IN))

        time.sleep(1.0)  # пережить остаток MODE_SLOW-стрима, если abort не сработал
        assert harness.say.goals_received == 0, "speak() не должен был вызваться -- ход оборван"

        harness.llm_server.mode = MockLlmServer.MODE_OK
        harness.llm_server.chunks_no_grammar = ["ок"]
        harness.llm_server.chunks_with_grammar = [_NOOP]
        _publish_transcript(client, "робот, ещё раз")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
    finally:
        harness.shutdown()


def test_pending_transcript_replayed_after_turn() -> None:
    """Реплика, пришедшая пока ход в полёте, не выбрасывается (живой баг «со
    второго раза»): текущий ход прерывается, реплика ждёт в слоте и
    отыгрывается сразу после -- следующий ход отвечает именно на неё."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        harness.llm_server.mode = MockLlmServer.MODE_SLOW
        harness.llm_server.chunks_with_grammar = [
            '{"think": "',
            "долго",
            " думаю",
            '"',
            ', "tool": "noop", "args": {}}',
        ]
        harness.llm_server.chunks_no_grammar = ["ок"]
        harness.llm_server.chunk_delay_s = 0.3

        client = harness.make_client_node()
        _publish_transcript(client, "робот, первая реплика")
        time.sleep(0.2)  # первый ход в полёте, стрим фазы действия идёт
        harness.llm_server.mode = MockLlmServer.MODE_OK  # реплей пройдёт быстро
        _publish_transcript(client, "робот, вторая реплика")

        def _second_answered() -> bool:
            body = harness.llm_server.last_request_body
            if not body:
                return False
            return any(
                m["role"] == "user" and m["content"].endswith("вторая реплика")
                for m in body.get("messages", [])
            )

        wait_until(_second_answered, timeout_s=10.0)

        # Первая (прерванная) реплика не потеряна -- лежит в истории реплея.
        body = harness.llm_server.last_request_body
        assert any(
            m["role"] == "user" and m["content"] == "первая реплика" for m in body["messages"]
        )
        # Прерванный ход не оставил пустой реплики робота в истории.
        entries = harness.dialog_agent._history._entries  # noqa: SLF001
        assert all(entry.text for entry in entries if entry.kind == "robot")
    finally:
        harness.shutdown()


def test_fast_path_confirm_suppresses_llm_call() -> None:
    """Уверенный локальный матч -- ЛЛМ вообще не запрашивается ни в одной из фаз."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool("tour_by_points", {"location_ids": ["stop0", "stop1"]})
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)
        wait_until(
            lambda: harness.dialog_agent.last_mission_state().state == _S.STATE_AWAITING_CONFIRM,
            timeout_s=5.0,
        )

        client = harness.make_client_node()
        _publish_transcript(client, "да, давайте")

        wait_until(_mission_state_is(harness, _S.STATE_NAVIGATING), timeout_s=5.0)
        assert harness.llm_server.last_request_body is None, "ЛЛМ не должен был вызываться вовсе"
    finally:
        harness.shutdown()


def test_idle_dismiss_suppresses_llm_call() -> None:
    """Живой баг: "робот стоп" в IDLE уходило в ЛЛМ и превращалось в галлюцинацию --
    fast-path обязан молча проглотить чистую команду отмены, ЛЛМ вообще не зовём."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        assert harness.dialog_agent.last_mission_state().state == _S.STATE_IDLE

        client = harness.make_client_node()
        _publish_transcript(client, "робот стоп")

        time.sleep(0.3)  # дать fast-path'у отработать, если он не сработает -- ход уйдёт в ЛЛМ
        assert harness.llm_server.last_request_body is None, "ЛЛМ не должен был вызываться вовсе"
        assert harness.say.goals_received == 0, "IDLE-dismiss не должен ничего озвучивать"
    finally:
        harness.shutdown()


def test_tour_end_transition_does_not_clear_history() -> None:
    """CLAUDE_CODE_TASK.md п.4 -- живой баг: тур остановлен, посетитель продолжает
    говорить про него, а история уже стёрта. Переход в IDLE из не-IDLE больше не
    чистит историю -- чистка остаётся только по отсутствию посетителя.

    Состояние `/mission/state` публикуется здесь напрямую (мимо реального
    `mission_fsm`/`Say`/`Narrate`/`NavigateToPose`) -- нужен только сам факт
    перехода не-IDLE -> IDLE, наблюдаемый `dialog_agent._on_mission_state()`;
    прогон настоящего тура не нужен и лишь добавляет гонки с реальными
    action-серверами, не относящиеся к проверяемому поведению."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        assert harness.dialog_agent.last_mission_state().state == _S.STATE_IDLE
        harness.say.chars_per_sec = 50.0

        harness.llm_server.chunks_no_grammar = ["Первый ответ до начала тура."]
        harness.llm_server.chunks_with_grammar = [_NOOP]
        client = harness.make_client_node()
        _publish_transcript(client, "робот, первый вопрос посетителя")
        wait_until(lambda: harness.say.goals_received >= 1, timeout_s=5.0)
        # Дождаться полного окончания ПЕРВОГО хода (не только speak()), иначе
        # второй транскрипт ниже может попасть на "ход уже в полёте" -- сброс
        # `_turn_in_flight` в finally может отстать от goals_received под
        # нагрузкой executor'а.
        wait_until(lambda: not harness.dialog_agent._turn_in_flight, timeout_s=10.0)  # noqa: SLF001

        state_pub = client.create_publisher(MissionState, "/mission/state", QOS_MISSION_STATE)
        wait_until(lambda: state_pub.get_subscription_count() >= 1, timeout_s=5.0)
        state_pub.publish(MissionState(state=_S.STATE_NARRATING, tour_id="full_tour"))
        wait_until(
            lambda: harness.dialog_agent.last_mission_state().state == _S.STATE_NARRATING,
            timeout_s=5.0,
        )
        state_pub.publish(MissionState(state=_S.STATE_IDLE))
        wait_until(
            lambda: harness.dialog_agent.last_mission_state().state == _S.STATE_IDLE,
            timeout_s=5.0,
        )

        harness.llm_server.chunks_no_grammar = ["Второй ответ после тура."]
        harness.llm_server.chunks_with_grammar = [_NOOP]
        _publish_transcript(client, "робот, второй вопрос посетителя")

        def _history_still_mentions_first_turn() -> bool:
            body = harness.llm_server.last_request_body
            if not body:
                return False
            return any(
                "первый вопрос посетителя" in m.get("content", "")
                for m in body.get("messages", [])
            )

        wait_until(_history_still_mentions_first_turn, timeout_s=5.0)
    finally:
        harness.shutdown()


def test_mission_state_transition_appends_history_event() -> None:
    """DIALOG_REWORK_PLAN.md §6.4: переход /mission/state порождает событие в истории --
    следующий ход должен увидеть его в отправленных ЛЛМ сообщениях."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        _setup_two_stop_tour(harness)
        started = harness.broker.call_tool("tour_by_points", {"location_ids": ["stop0", "stop1"]})
        assert started.ok, started.message

        pump_clock(harness.clock, _mission_state_is(harness, _S.STATE_AWAITING_CONFIRM), step=0.1)
        wait_until(
            lambda: harness.dialog_agent.last_mission_state().state == _S.STATE_AWAITING_CONFIRM,
            timeout_s=5.0,
        )

        harness.llm_server.chunks_no_grammar = ["Хорошо."]
        harness.llm_server.chunks_with_grammar = [_NOOP]

        client = harness.make_client_node()
        _publish_transcript(client, "а что тут интересного вообще")

        def _sent_history_mentions_transition() -> bool:
            body = harness.llm_server.last_request_body
            if not body:
                return False
            return any(
                "СОБЫТИЕ" in m.get("content", "") and "AWAITING_CONFIRM" in m.get("content", "")
                for m in body.get("messages", [])
            )

        wait_until(_sent_history_mentions_transition, timeout_s=5.0)
    finally:
        harness.shutdown()
