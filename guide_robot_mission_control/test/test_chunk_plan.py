"""ChunkPlan: учёт произнесённого и инвариант полноты (design §3.3)."""

from __future__ import annotations

import random
import string

import pytest

from guide_robot_mission_control.chunk_plan import ChunkPlan, ChunkState
from guide_robot_mission_control.resume import (
    ResumeOutcome,
    ResumePolicy,
    ResumeToken,
    apply_resume_policy,
    resolve_resume,
)

EXHIBIT_ID = "lab105a"
VERSION = "deadbeef"


def _make_chunks(rng: random.Random, count: int) -> list[str]:
    """Различимые непустые тексты чанков -- чтобы отследить полноту/порядок."""
    return [
        f"c{i}:" + "".join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 20)))
        for i in range(count)
    ]


def _run_session(
    rng: random.Random, chunks: list[str], policies: list[ResumePolicy]
) -> tuple[ChunkPlan, set[int]]:
    """Прогнать один нарратив до конца, интерпируя случайно на каждом чанке.

    Возвращает финальный ChunkPlan (is_complete() == True) и множество
    индексов, которые continue_next признал потерянными за сессию.
    """
    all_skipped: set[int] = set()
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks)

    while True:
        interrupted_at: int | None = None
        idx = plan.next_to_send()
        while idx is not None:
            plan.mark(idx, ChunkState.SENT)
            plan.mark(idx, ChunkState.SPEAKING)
            # 40% шанс оборвать чанк на случайном офсете, а не доиграть.
            if rng.random() < 0.4:
                text = plan.chunk_text(idx)
                cut_at = rng.randint(0, len(text))
                plan.mark(idx, ChunkState.CUT, spoken_chars=cut_at)
                interrupted_at = idx
                break
            plan.mark(idx, ChunkState.DONE)
            idx = plan.next_to_send()

        if interrupted_at is None:
            assert plan.is_complete()
            return plan, all_skipped

        token = ResumeToken.parse(plan.resume_token())
        decision = resolve_resume(
            token, exhibit_id=EXHIBIT_ID, version=VERSION, chunk_count=plan.chunk_total
        )
        assert decision.outcome is ResumeOutcome.RESUME

        policy = policies[interrupted_at % len(policies)]
        decision = apply_resume_policy(decision, policy, chunk_count=plan.chunk_total)

        if decision.skipped_chunk_idx is not None:
            all_skipped.add(decision.skipped_chunk_idx)

        if decision.outcome is ResumeOutcome.ALREADY_COMPLETE:
            plan = ChunkPlan(
                EXHIBIT_ID,
                VERSION,
                chunks,
                start_idx=decision.start_chunk_idx,
                skipped=all_skipped,
            )
            assert plan.is_complete()
            return plan, all_skipped

        plan = ChunkPlan(
            EXHIBIT_ID,
            VERSION,
            chunks,
            start_idx=decision.start_chunk_idx,
            skipped=all_skipped,
        )


@pytest.mark.parametrize("seed", range(200))
@pytest.mark.parametrize(
    "policy_set",
    [
        [ResumePolicy.REPEAT_CHUNK],
        [ResumePolicy.OVERLAP_1],
        [ResumePolicy.CONTINUE_NEXT],
    ],
)
def test_completeness_invariant_survives_random_interruptions(
    seed: int, policy_set: list[ResumePolicy]
) -> None:
    """Для любой последовательности прерываний внутри одной политики: ни один
    непропущенный чанк не потерян, порядок сохранён, continue_next пропускает
    не более одного чанка за прерывание.

    Политика -- один параметр narration_server на весь его жизненный цикл
    (config/mission.yaml: resume_policy), не выбор на лету по чанку; отсюда
    один фиксированный policy_set на сессию, а не смесь -- смесь дала бы
    overlap_1 шанс "воскресить" уже готовый continue_next-скип, а это не
    определено ни design §3.2, ни реальным поведением узла.
    """
    rng = random.Random(seed)
    chunks = _make_chunks(rng, rng.randint(1, 8))

    plan, skipped = _run_session(rng, chunks, policy_set)

    final_text = plan.spoken_text()
    last_pos = -1
    for idx, chunk_text in enumerate(chunks):
        if idx in skipped:
            assert chunk_text not in final_text or chunk_text == "", (
                f"пропущенный чанк {idx} не должен фигурировать в итоговом тексте"
            )
            continue
        pos = final_text.find(chunk_text, last_pos + 1)
        assert pos != -1, f"чанк {idx} потерян: {chunk_text!r} отсутствует в {final_text!r}"
        assert pos > last_pos, f"чанк {idx} нарушает порядок"
        last_pos = pos

    if ResumePolicy.CONTINUE_NEXT not in policy_set:
        assert not skipped


def test_repeat_chunk_default_replays_whole_interrupted_chunk() -> None:
    """repeat_chunk (дефолт): прерванный чанк озвучивается заново с начала, не с char_off."""
    chunks = ["Первое предложение.", "Второе, подлиннее предложение.", "Третье."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks)

    plan.mark(0, ChunkState.DONE)
    plan.mark(1, ChunkState.SENT)
    plan.mark(1, ChunkState.SPEAKING)
    plan.mark(1, ChunkState.CUT, spoken_chars=10)

    token = ResumeToken.parse(plan.resume_token())
    assert token == ResumeToken(EXHIBIT_ID, VERSION, 1, 10)

    decision = resolve_resume(token, exhibit_id=EXHIBIT_ID, version=VERSION, chunk_count=3)
    decision = apply_resume_policy(decision, ResumePolicy.REPEAT_CHUNK, chunk_count=3)
    assert decision.start_chunk_idx == 1

    resumed = ChunkPlan(EXHIBIT_ID, VERSION, chunks, start_idx=decision.start_chunk_idx)
    assert resumed.state_of(1) == ChunkState.PENDING
    assert resumed.state_of(0) == ChunkState.DONE


def test_interruption_on_last_chunk() -> None:
    chunks = ["Один.", "Два.", "Три."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks)
    plan.mark(0, ChunkState.DONE)
    plan.mark(1, ChunkState.DONE)
    plan.mark(2, ChunkState.SENT)
    plan.mark(2, ChunkState.CUT, spoken_chars=2)

    token = ResumeToken.parse(plan.resume_token())
    assert token == ResumeToken(EXHIBIT_ID, VERSION, 2, 2)
    decision = resolve_resume(token, exhibit_id=EXHIBIT_ID, version=VERSION, chunk_count=3)
    assert decision.outcome is ResumeOutcome.RESUME
    assert decision.start_chunk_idx == 2


def test_double_interruption_in_a_row() -> None:
    """Прерывание сразу после прерывания -- второй Narrate goal тоже не доигрывает."""
    chunks = ["Alpha.", "Beta.", "Gamma.", "Delta."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks)
    plan.mark(0, ChunkState.DONE)
    plan.mark(1, ChunkState.SENT)
    plan.mark(1, ChunkState.CUT, spoken_chars=1)

    decision = resolve_resume(
        ResumeToken.parse(plan.resume_token()),
        exhibit_id=EXHIBIT_ID,
        version=VERSION,
        chunk_count=4,
    )
    decision = apply_resume_policy(decision, ResumePolicy.REPEAT_CHUNK, chunk_count=4)
    plan2 = ChunkPlan(EXHIBIT_ID, VERSION, chunks, start_idx=decision.start_chunk_idx)
    assert plan2.state_of(1) == ChunkState.PENDING

    plan2.mark(1, ChunkState.SENT)
    plan2.mark(1, ChunkState.CUT, spoken_chars=0)

    decision2 = resolve_resume(
        ResumeToken.parse(plan2.resume_token()),
        exhibit_id=EXHIBIT_ID,
        version=VERSION,
        chunk_count=4,
    )
    assert decision2.outcome is ResumeOutcome.RESUME
    assert decision2.start_chunk_idx == 1


def test_race_interruption_after_sent_before_started() -> None:
    """Прерывание в момент SENT, но до started (гонка lookahead): chunk_idx верный."""
    chunks = ["Alpha.", "Beta."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks, lookahead=1)
    plan.mark(0, ChunkState.SENT)
    idx = plan.next_to_send()
    assert idx == 1  # lookahead=1 -- второй чанк уже можно слать, пока первый ещё SENT
    plan.mark(1, ChunkState.SENT)
    # оба в полёте, ни один ещё не начал звучать -- barge-in режет оба как CUT с 0 символов
    plan.mark(0, ChunkState.CUT, spoken_chars=0)
    plan.mark(1, ChunkState.CUT, spoken_chars=0)

    token = ResumeToken.parse(plan.resume_token())
    assert token == ResumeToken(EXHIBIT_ID, VERSION, 0, 0)


def test_next_to_send_respects_lookahead_zero() -> None:
    chunks = ["Alpha.", "Beta.", "Gamma."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks, lookahead=0)
    idx = plan.next_to_send()
    assert idx == 0
    plan.mark(0, ChunkState.SENT)
    assert plan.next_to_send() is None  # уже один в полёте -- при lookahead=0 второй не шлём
    plan.mark(0, ChunkState.DONE)
    assert plan.next_to_send() == 1


def test_next_to_send_respects_lookahead_one() -> None:
    chunks = ["Alpha.", "Beta.", "Gamma."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks, lookahead=1)
    plan.mark(0, ChunkState.SENT)
    assert plan.next_to_send() == 1
    plan.mark(1, ChunkState.SENT)
    assert plan.next_to_send() is None  # 2 в полёте > lookahead=1


def test_skipped_chunk_absent_from_spoken_text_and_progress() -> None:
    chunks = ["Alpha.", "Beta.", "Gamma."]
    plan = ChunkPlan(EXHIBIT_ID, VERSION, chunks, start_idx=2, skipped={1})
    assert plan.state_of(0) == ChunkState.DONE
    assert plan.state_of(1) == ChunkState.SKIPPED
    assert plan.state_of(2) == ChunkState.PENDING
    assert "Beta." not in plan.spoken_text()
    assert plan.spoken_text() == "Alpha."
    assert plan.chunks_spoken() == 1
    assert plan.progress() < 1.0

    plan.mark(2, ChunkState.DONE)
    assert plan.is_complete()
    assert plan.chunks_spoken() == 2
