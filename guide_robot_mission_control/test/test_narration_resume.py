"""Резюмирование нарратива через реальный narration_server (design §9.2).

Уровень чанка/resume-грамматики уже исчерпывающе проверен без ROS в
test_chunk_plan.py (property-based, 200 сидов) и test_resume_token.py --
здесь не повторяем тот же инвариант руками для каждого combo, а проверяем,
что реальная ROS-обвязка (Narrate action, NarrationControl service,
epoch-fencing через CancelAll, resume_token, прошедший через ДВА отдельных
Narrate-вызова) работает так же, как чистая логика под ней предсказывает.

Синхронизация с sim-временем -- намеренно ПОШАГОВАЯ, не единым большим
прыжком и не поллингом мелкими 0.05-секундными шагами:

  1. Продвижение чанка k -> k+1 в моке зависит от РЕАЛЬНОГО времени
     (FIFO-очередь Say разбирается поллингом `_wait_for_turn`, не по
     /clock), а вот ЗАВЕРШЕНИЕ конкретного чанка зависит от sim-времени
     (`elapsed = now() - start`, где `start` -- момент промоции ИМЕННО
     этого чанка в активные, не начала теста).
  2. Значит один прыжок часов продвигает РОВНО ОДИН переход: уже активный
     чанк дозвучивает и завершается, а следующий (только что промоченный,
     start=текущее время) сразу замирает на elapsed=0 -- часы-то больше не
     двигались. Чтобы добраться до чанка k, нужно ровно k прыжков по
     длительности одного чанка, каждый со settle-паузой на реальном
     времени между ними.
  3. Ранняя версия этого файла молотила по /clock мелкими шагами (0.05 с)
     в тесном цикле реального времени -- под MultiThreadedExecutor с
     дюжиной долго поллящих потоков (Say-мок, его очередь,
     narration_server) это гоняло GIL так интенсивно, что конкретные
     потоки годами не получали кванта времени (наблюдалось
     воспроизводимо даже на 22 ядрах) -- тесты либо зависали, либо ловили
     таймаут пампа. Точные прыжки "по одному чанку" на порядок сокращают
     число итераций и делают тесты быстрыми и стабильными.
"""

from __future__ import annotations

import time

import pytest
from guide_robot_msgs.action import Narrate
from guide_robot_msgs.msg import CancelAll, SpeakingStatus
from guide_robot_msgs.srv import NarrationControl
from rclpy.action import ActionClient
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.parameter import Parameter
from rclpy.task import Future

from guide_robot_mission_control.lib.qos import QOS_VOICE_SPEAKING
from guide_robot_mission_control.narration_server_node import NarrationServerNode
from guide_robot_mission_control.resume import ResumePolicy, ResumeToken
from test.mocks.harness import MissionTestHarness, wait_for_future, wait_until

CHUNKS = ["Раз.", "Два.", "Три."]
CHUNK_LEN = len(CHUNKS[0])
assert all(len(c) == CHUNK_LEN for c in CHUNKS), (
    "хелперы синхронизации ниже предполагают равную длину"
)

_WAIT_TIMEOUT_S = 15.0


@pytest.fixture
def harness():
    h = MissionTestHarness()
    yield h
    h.shutdown()


def _make_node(harness: MissionTestHarness, **overrides: object) -> NarrationServerNode:
    params = [Parameter("use_sim_time", value=True)]
    params.extend(Parameter(name, value=value) for name, value in overrides.items())
    node = NarrationServerNode(context=harness.context, parameter_overrides=params)
    harness.add_node(node)
    assert node.trigger_configure() == TransitionCallbackReturn.SUCCESS
    assert node.trigger_activate() == TransitionCallbackReturn.SUCCESS
    return node


def _chunk_duration_s(harness: MissionTestHarness) -> float:
    return CHUNK_LEN / harness.say.chars_per_sec


def _advance_to_chunk_started(
    harness: MissionTestHarness,
    speaking_count: list[int],
    k: int,
    *,
    baseline: int = 0,
    margin: float = 0.05,
) -> None:
    """Довести до состояния "чанк k говорит, но не закончил" -- см. докстринг модуля.

    `baseline` -- значение speaking_count перед стартом ЭТОГО эпизода
    озвучки (нужно, если тот же счётчик уже накопил переходы от
    предыдущего Narrate-вызова в той же сессии, например в
    test_double_interrupt_then_resume_completes).
    """
    wait_until(lambda: speaking_count[0] >= baseline + 1, timeout_s=_WAIT_TIMEOUT_S)
    for i in range(k):
        harness.clock.advance(_chunk_duration_s(harness) + margin)
        target = baseline + i + 2
        wait_until(lambda target=target: speaking_count[0] >= target, timeout_s=_WAIT_TIMEOUT_S)


def _drain_to_completion(
    harness: MissionTestHarness,
    future: Future,
    *,
    max_iterations: int = 16,
    margin: float = 0.05,
    settle_s: float = 0.05,
) -> None:
    """Пока future не готов -- поджимать часы на длительность одного чанка.

    Не единый большой прыжок: следующий чанк каскада замирает на elapsed=0
    сразу после промоции (см. докстринг модуля), поэтому нужно опять же по
    одному прыжку на переход. max_iterations -- с большим запасом на план
    (chunk_total) плюс возможный resume-мостик.
    """
    for _ in range(max_iterations):
        if future.done():
            break
        harness.clock.advance(_chunk_duration_s(harness) + margin)
        time.sleep(settle_s)
    wait_for_future(future, timeout_s=_WAIT_TIMEOUT_S)


def _speaking_started_count(client_node) -> list[int]:
    """Число РАЗНЫХ goal_id, замеченных с speaking=True.

    Не счётчик фронтов False->True: /voice/speaking живёт на QoS depth=1
    (design §7), и при двух публикациях подряд быстрее, чем подписчик
    успевает их разобрать (чанк k освобождает слот и чанк k+1 тут же его
    занимает), промежуточное speaking=False может быть вытеснено из
    очереди раньше доставки -- подписчик увидит только True->True и ни
    разу не зафиксирует фронт. Считать по множеству увиденных goal_id
    устойчиво к этому: не важно, сколько сообщений потерялось между ними.
    """
    seen_goal_ids: set[str] = set()
    count = [0]

    def _on_status(msg: SpeakingStatus) -> None:
        if msg.speaking and msg.goal_id not in seen_goal_ids:
            seen_goal_ids.add(msg.goal_id)
            count[0] = len(seen_goal_ids)

    client_node.create_subscription(
        SpeakingStatus, "/voice/speaking", _on_status, QOS_VOICE_SPEAKING
    )
    return count


def _make_clients(harness: MissionTestHarness):
    client_node = harness.make_client_node()
    narrate_client = ActionClient(client_node, Narrate, "narrate")
    assert narrate_client.wait_for_server(timeout_sec=5.0)
    control_client = client_node.create_client(NarrationControl, "/narration_server/control")
    assert control_client.wait_for_service(timeout_sec=5.0)
    return client_node, narrate_client, control_client


def test_narrate_completes_without_interruption(harness: MissionTestHarness) -> None:
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(harness, lookahead=1, resume_bridge_enabled=False)
    harness.say.chars_per_sec = 10.0
    _client_node, narrate_client, _control_client = _make_clients(harness)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    result_future = goal_handle.get_result_async()
    _drain_to_completion(harness, result_future)
    result: Narrate.Result = result_future.result().result

    assert result.outcome == Narrate.Result.OUTCOME_COMPLETED
    assert result.chunks_spoken == len(CHUNKS)
    assert result.chunks_total == len(CHUNKS)
    assert result.spoken_text == " ".join(CHUNKS)


@pytest.mark.parametrize("lookahead", [0, 1])
@pytest.mark.parametrize(
    "resume_policy",
    [
        ResumePolicy.REPEAT_CHUNK.value,
        ResumePolicy.CONTINUE_NEXT.value,
        ResumePolicy.OVERLAP_1.value,
    ],
)
@pytest.mark.parametrize("k", range(len(CHUNKS)))
def test_hard_interrupt_at_each_chunk_then_resume_completes(
    harness: MissionTestHarness, k: int, resume_policy: str, lookahead: int
) -> None:
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(
        harness,
        lookahead=lookahead,
        resume_policy=resume_policy,
        resume_bridge_enabled=False,
        hard_stop_result_timeout_s=1.0,
    )
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, control_client = _make_clients(harness)
    speaking_count = _speaking_started_count(client_node)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    assert goal_handle.accepted

    _advance_to_chunk_started(harness, speaking_count, k)

    epoch_before = harness.say.epoch
    control_future = control_client.call_async(
        NarrationControl.Request(mode=NarrationControl.Request.MODE_HARD, reason="test")
    )
    wait_for_future(control_future, timeout_s=_WAIT_TIMEOUT_S)
    assert control_future.result().ok

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
    first_result: Narrate.Result = result_future.result().result

    assert first_result.outcome == Narrate.Result.OUTCOME_INTERRUPTED
    assert first_result.chunks_total == len(CHUNKS)
    assert harness.say.epoch > epoch_before

    token = ResumeToken.parse(first_result.resume_token)
    assert token is not None
    assert token.chunk_idx == k

    harness.say.chars_per_sec = 50.0
    second_goal_future = narrate_client.send_goal_async(
        Narrate.Goal(exhibit_id="lab105a", resume_token=first_result.resume_token)
    )
    wait_for_future(second_goal_future)
    second_handle = second_goal_future.result()
    assert second_handle.accepted

    second_result_future = second_handle.get_result_async()
    _drain_to_completion(harness, second_result_future)
    second_result: Narrate.Result = second_result_future.result().result

    assert second_result.outcome == Narrate.Result.OUTCOME_COMPLETED
    assert second_result.chunks_total == len(CHUNKS)


def test_interrupt_races_sent_before_started(harness: MissionTestHarness) -> None:
    """lookahead=1: чанк 1 может уйти в SENT раньше чанка 0 -- resume должен указать на 0."""
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(harness, lookahead=1, resume_bridge_enabled=False, hard_stop_result_timeout_s=1.0)
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, control_client = _make_clients(harness)
    speaking_count = _speaking_started_count(client_node)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()

    _advance_to_chunk_started(harness, speaking_count, 0)
    time.sleep(0.02)  # дать конвейеру шанс отправить lookahead-чанк 1 (SENT)

    control_future = control_client.call_async(
        NarrationControl.Request(mode=NarrationControl.Request.MODE_HARD, reason="test")
    )
    wait_for_future(control_future, timeout_s=_WAIT_TIMEOUT_S)

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
    result: Narrate.Result = result_future.result().result

    assert result.outcome == Narrate.Result.OUTCOME_INTERRUPTED
    token = ResumeToken.parse(result.resume_token)
    assert token is not None
    assert token.chunk_idx == 0


def test_double_interrupt_then_resume_completes(harness: MissionTestHarness) -> None:
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(
        harness,
        lookahead=0,
        resume_policy=ResumePolicy.REPEAT_CHUNK.value,
        resume_bridge_enabled=False,
        hard_stop_result_timeout_s=1.0,
    )
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, control_client = _make_clients(harness)
    speaking_count = _speaking_started_count(client_node)

    resume_token = ""
    for expected_chunk_idx in (0, 1):
        baseline = speaking_count[0]
        goal_future = narrate_client.send_goal_async(
            Narrate.Goal(exhibit_id="lab105a", resume_token=resume_token)
        )
        wait_for_future(goal_future)
        goal_handle = goal_future.result()
        assert goal_handle.accepted

        # repeat_chunk (дефолт) всегда возобновляет план заново с
        # start_idx=0 -- индексы ЭТОГО episode'а совпадают с глобальными
        # индексами чанков, поэтому k тот же expected_chunk_idx.
        _advance_to_chunk_started(harness, speaking_count, expected_chunk_idx, baseline=baseline)

        control_future = control_client.call_async(
            NarrationControl.Request(mode=NarrationControl.Request.MODE_HARD, reason="test")
        )
        wait_for_future(control_future, timeout_s=_WAIT_TIMEOUT_S)

        result_future = goal_handle.get_result_async()
        wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
        result: Narrate.Result = result_future.result().result
        assert result.outcome == Narrate.Result.OUTCOME_INTERRUPTED

        token = ResumeToken.parse(result.resume_token)
        assert token is not None
        assert token.chunk_idx == expected_chunk_idx
        resume_token = result.resume_token

    harness.say.chars_per_sec = 50.0
    final_goal_future = narrate_client.send_goal_async(
        Narrate.Goal(exhibit_id="lab105a", resume_token=resume_token)
    )
    wait_for_future(final_goal_future)
    final_handle = final_goal_future.result()
    assert final_handle.accepted

    final_result_future = final_handle.get_result_async()
    _drain_to_completion(harness, final_result_future)
    final_result: Narrate.Result = final_result_future.result().result

    assert final_result.outcome == Narrate.Result.OUTCOME_COMPLETED
    assert final_result.chunks_total == len(CHUNKS)


def test_hard_stop_timeout_fallback_does_not_hang_when_say_never_returns(
    harness: MissionTestHarness,
) -> None:
    """Say никогда не вернёт результат -- hard-stop обязан уложиться в таймаут и не зависнуть."""
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    harness.say.never_return_result.add(CHUNKS[0])
    _make_node(harness, lookahead=0, resume_bridge_enabled=False, hard_stop_result_timeout_s=0.1)
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, control_client = _make_clients(harness)
    speaking_count = _speaking_started_count(client_node)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()

    _advance_to_chunk_started(harness, speaking_count, 0)

    control_future = control_client.call_async(
        NarrationControl.Request(mode=NarrationControl.Request.MODE_HARD, reason="test")
    )
    # never_return_result -- это "бэкенд вообще не начал синтез", он не
    # шлёт feedback вообще (см. докстринг мока: сразу уходит в вечный сон,
    # минуя _pace()). Поэтому у _do_hard_stop нет НИКАКИХ данных о
    # прогрессе -- фолбэк корректно даёт char_off=0, не какое-то
    # "последнее известное" значение > 0. Он же игнорирует даже
    # cancel_goal_async(), так что _do_hard_stop свалится в свой sim-time
    # таймаут-фолбэк, а он меряется по /clock -- без продвижения часов
    # elapsed внутри _do_hard_stop навечно останется 0, и control никогда
    # не ответит.
    _drain_to_completion(harness, control_future)

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
    result: Narrate.Result = result_future.result().result

    assert result.outcome == Narrate.Result.OUTCOME_INTERRUPTED
    token = ResumeToken.parse(result.resume_token)
    assert token is not None
    assert token.chunk_idx == 0
    assert token.char_off == 0


def test_barge_in_via_cancel_all_hard_stops_without_control_service(
    harness: MissionTestHarness,
) -> None:
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(harness, lookahead=0, resume_bridge_enabled=False, hard_stop_result_timeout_s=1.0)
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, _control_client = _make_clients(harness)
    cancel_all_pub = client_node.create_publisher(CancelAll, "/speech/cancel_all", 1)
    speaking_count = _speaking_started_count(client_node)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()

    _advance_to_chunk_started(harness, speaking_count, 0)

    epoch_before = harness.say.epoch
    cancel_all_pub.publish(
        CancelAll(scope=CancelAll.SCOPE_NARRATION, reason=CancelAll.REASON_BARGE_IN)
    )

    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
    result: Narrate.Result = result_future.result().result

    assert result.outcome == Narrate.Result.OUTCOME_INTERRUPTED
    assert result.detail == "barge_in"
    # narration_server не обязан бампать epoch повторно -- barge-in уже сделал это сам.
    assert harness.say.epoch == epoch_before + 1


def test_resume_bridge_phrase_sent_before_resumed_chunks(harness: MissionTestHarness) -> None:
    harness.fixtures.add_exhibit("lab105a", CHUNKS, version="rev1")
    _make_node(
        harness,
        lookahead=0,
        resume_bridge_enabled=True,
        resume_bridge_text="Продолжаю.",
        hard_stop_result_timeout_s=1.0,
    )
    harness.say.chars_per_sec = 10.0
    client_node, narrate_client, control_client = _make_clients(harness)
    speaking_count = _speaking_started_count(client_node)

    goal_future = narrate_client.send_goal_async(Narrate.Goal(exhibit_id="lab105a"))
    wait_for_future(goal_future)
    goal_handle = goal_future.result()
    # Мостик шлётся только если plan.chunks_spoken() > 0 у ВОЗОБНОВЛЁННОГО
    # плана (§3.2: мостик для НЕПУСТОГО "продолжаю", не для чистого
    # старта) -- значит прерывать нужно не на нулевом чанке (тогда
    # start_idx=0 и до него ничего DONE), а на первом, чтобы к моменту
    # возобновления chunk 0 уже был отмечен DONE.
    _advance_to_chunk_started(harness, speaking_count, 1)

    control_future = control_client.call_async(
        NarrationControl.Request(mode=NarrationControl.Request.MODE_HARD, reason="test")
    )
    wait_for_future(control_future, timeout_s=_WAIT_TIMEOUT_S)
    result_future = goal_handle.get_result_async()
    wait_for_future(result_future, timeout_s=_WAIT_TIMEOUT_S)
    first_result: Narrate.Result = result_future.result().result
    assert first_result.outcome == Narrate.Result.OUTCOME_INTERRUPTED

    # Считаем переходы заново -- мостик тоже должен дать восходящий фронт.
    speaking_count_resume = _speaking_started_count(client_node)

    harness.say.chars_per_sec = 50.0
    second_goal_future = narrate_client.send_goal_async(
        Narrate.Goal(exhibit_id="lab105a", resume_token=first_result.resume_token)
    )
    wait_for_future(second_goal_future)
    second_handle = second_goal_future.result()

    second_result_future = second_handle.get_result_async()
    _drain_to_completion(harness, second_result_future)
    second_result: Narrate.Result = second_result_future.result().result

    assert second_result.outcome == Narrate.Result.OUTCOME_COMPLETED
    # Мостик + повторно отправленный (repeat_chunk) чанк 1 + чанк 2 -- 3 восходящих фронта.
    assert speaking_count_resume[0] == 3
