"""Юниты на приоритетную очередь высказываний."""

from __future__ import annotations

from guide_robot_voice.lib.scheduler import Action, Scheduler, Scope, Utterance


def make(goal_id: str, priority: int, seq: int, **kwargs: object) -> Utterance:
    """Собрать заявку с умолчаниями."""
    return Utterance(goal_id=goal_id, text="x", priority=priority, seq=seq, **kwargs)  # type: ignore[arg-type]


def test_first_goal_starts() -> None:
    """Первая заявка исполняется немедленно."""
    scheduler = Scheduler()
    assert scheduler.submit(make("a", 50, 0)).action is Action.START


def test_equal_priority_queues() -> None:
    """Равный приоритет не вытесняет."""
    scheduler = Scheduler()
    scheduler.submit(make("a", 50, 0))
    assert scheduler.submit(make("b", 50, 1)).action is Action.QUEUE


def test_higher_priority_preempts() -> None:
    """Более приоритетная заявка вытесняет текущую."""
    scheduler = Scheduler()
    scheduler.submit(make("narration", 50, 0))
    decision = scheduler.submit(make("dialog", 100, 1))
    assert decision.action is Action.PREEMPT
    assert decision.victim is not None
    assert decision.victim.goal_id == "narration"


def test_non_interruptible_survives_higher_priority() -> None:
    """Аварийную фразу не вытесняет даже более приоритетная заявка."""
    scheduler = Scheduler()
    scheduler.submit(make("safety", 200, 0, interruptible=False, scope=Scope.SAFETY))
    decision = scheduler.submit(make("louder", 250, 1))
    assert decision.action is Action.QUEUE


def test_fifo_within_priority() -> None:
    """Внутри одного приоритета порядок сохраняется."""
    scheduler = Scheduler()
    scheduler.submit(make("a", 50, 0))
    scheduler.submit(make("b", 50, 1))
    scheduler.submit(make("c", 50, 2))
    assert [u.goal_id for u in scheduler.queued] == ["b", "c"]


def test_promotion_respects_priority() -> None:
    """После завершения активной выбирается самая приоритетная из очереди."""
    scheduler = Scheduler()
    scheduler.submit(make("active", 200, 0, interruptible=False))
    scheduler.submit(make("low", 10, 1))
    scheduler.submit(make("mid", 90, 2))
    nxt = scheduler.finish("active")
    assert nxt is not None
    assert nxt.goal_id == "mid"


def test_preempted_goal_is_not_requeued() -> None:
    """Вытесненная цель не возвращается в очередь.

    Решение о возобновлении принимает narration_server: посетитель мог
    задать вопрос, после которого остаток справки не нужен.
    """
    scheduler = Scheduler()
    scheduler.submit(make("narration", 50, 0))
    decision = scheduler.submit(make("dialog", 100, 1))
    assert decision.action is Action.PREEMPT
    assert "narration" not in [u.goal_id for u in scheduler.queued]
    assert scheduler.finish("dialog") is None


def test_scope_cancel_is_selective() -> None:
    """Отмена по scope не задевает чужой scope."""
    scheduler = Scheduler()
    scheduler.submit(make("narr", 50, 0, scope=Scope.NARRATION))
    scheduler.submit(make("dlg", 40, 1, scope=Scope.DIALOG))
    dropped_active, dropped_queue = scheduler.cancel(Scope.NARRATION)
    assert dropped_active is not None and dropped_active.goal_id == "narr"
    assert dropped_queue == []
    assert scheduler.active is not None and scheduler.active.goal_id == "dlg"


def test_safety_scope_cancels_non_interruptible() -> None:
    """SAFETY-отмена гасит даже неprerываемое: это путь E-Stop."""
    scheduler = Scheduler()
    scheduler.submit(make("safety", 200, 0, interruptible=False, scope=Scope.SAFETY))
    dropped_active, _ = scheduler.cancel(Scope.SAFETY)
    assert dropped_active is not None


def test_queue_full_rejects() -> None:
    """Переполнение очереди отклоняет заявку, а не растёт неограниченно."""
    scheduler = Scheduler(max_queue=2)
    scheduler.submit(make("a", 50, 0))
    scheduler.submit(make("b", 50, 1))
    scheduler.submit(make("c", 50, 2))
    assert scheduler.submit(make("d", 50, 3)).action is Action.REJECT


def test_non_interruptible_survives_matching_scope_cancel() -> None:
    """Non-interruptible активная цель переживает обычную отмену её scope.

    Барьер interruptible защищает не только от вытеснения приоритетом,
    но и от /speech/cancel_all -- за исключением e-stop, см. ниже.
    """
    scheduler = Scheduler()
    scheduler.submit(make("safety", 200, 0, interruptible=False, scope=Scope.DIALOG))
    dropped_active, _ = scheduler.cancel(Scope.DIALOG, reason="barge_in")
    assert dropped_active is None
    assert scheduler.active is not None and scheduler.active.goal_id == "safety"


def test_estop_reason_cancels_non_interruptible_outside_safety_scope() -> None:
    """reason=REASON_ESTOP гасит non-interruptible, даже если scope не SAFETY.

    Design: "interruptible == false защищает ... но не от CancelAll
    с reason=REASON_ESTOP или scope=SCOPE_SAFETY" -- это ИЛИ, не совпадение
    с scope=SAFETY обязательно.
    """
    scheduler = Scheduler()
    scheduler.submit(make("warning", 200, 0, interruptible=False, scope=Scope.DIALOG))
    dropped_active, _ = scheduler.cancel(Scope.DIALOG, reason="estop")
    assert dropped_active is not None
    assert dropped_active.goal_id == "warning"


def test_non_interruptible_in_queue_survives_soft_cancel() -> None:
    """Барьер interruptible действует и на очередь, не только на активную цель."""
    scheduler = Scheduler()
    scheduler.submit(make("active", 200, 0, interruptible=False, scope=Scope.SAFETY))
    scheduler.submit(make("queued", 150, 1, interruptible=False, scope=Scope.DIALOG))
    _, dropped_queue = scheduler.cancel(Scope.DIALOG, reason="barge_in")
    assert dropped_queue == []
    assert [u.goal_id for u in scheduler.queued] == ["queued"]


def test_estop_clears_queue_regardless_of_interruptible() -> None:
    """e-stop -- единственный путь, который не должен оставлять хвостов."""
    scheduler = Scheduler()
    scheduler.submit(make("active", 200, 0, interruptible=False, scope=Scope.SAFETY))
    scheduler.submit(make("queued", 150, 1, interruptible=False, scope=Scope.DIALOG))
    dropped_active, dropped_queue = scheduler.cancel(Scope.ALL, reason="estop")
    assert dropped_active is not None
    assert [u.goal_id for u in dropped_queue] == ["queued"]
    assert scheduler.active is None
    assert scheduler.queued == ()
