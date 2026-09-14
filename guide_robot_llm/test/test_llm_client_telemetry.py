"""`llm_client.telemetry.ClientTelemetry` -- тайминги стадий и счётчики (Taiga #3)."""

from __future__ import annotations

import threading

from guide_robot_llm.llm_client.telemetry import (
    STAGE_GENERATION,
    STAGE_NETWORK,
    ClientTelemetry,
    StageTimings,
)


def test_initial_snapshot_empty() -> None:
    snap = ClientTelemetry().snapshot()

    assert snap["attempts"] == 0
    assert snap["retries"] == 0
    assert snap["fallbacks"] == 0
    assert snap["skipped_incompatible"] == 0
    assert snap["http_failures"] == 0
    assert snap["timeouts"] == 0
    assert snap["failure_stages"] == {}
    assert snap["last_failure"] is None
    assert snap["timings"] == []


def test_failure_counts_and_stage_attribution() -> None:
    tel = ClientTelemetry()
    tel.record_attempt()
    tel.record_timeout(STAGE_NETWORK)
    tel.record_retry()
    tel.record_attempt()
    tel.record_http_failure(STAGE_GENERATION)

    snap = tel.snapshot()

    assert snap["attempts"] == 2
    assert snap["retries"] == 1
    assert snap["http_failures"] == 1
    assert snap["timeouts"] == 1
    assert snap["failure_stages"] == {STAGE_NETWORK: 1, STAGE_GENERATION: 1}
    assert snap["last_failure"] == {"stage": STAGE_GENERATION, "kind": "http"}


def test_fallback_and_incompatible_counts() -> None:
    tel = ClientTelemetry()
    tel.record_fallback()
    tel.record_skipped_incompatible()
    tel.record_skipped_incompatible()

    snap = tel.snapshot()

    assert snap["fallbacks"] == 1
    assert snap["skipped_incompatible"] == 2


def test_success_timings_recorded() -> None:
    tel = ClientTelemetry()
    tel.record_success(
        StageTimings(
            serialization_ms=1.0,
            upload_ms=10.0,
            ttft_ms=120.0,
            full_ms=900.0,
            parse_ready_ms=540.0,
        )
    )

    snap = tel.snapshot()

    assert snap["timings"] == [
        {
            "serialization_ms": 1.0,
            "upload_ms": 10.0,
            "ttft_ms": 120.0,
            "full_ms": 900.0,
            "parse_ready_ms": 540.0,
        }
    ]


def test_reset_clears_everything() -> None:
    tel = ClientTelemetry()
    tel.record_attempt()
    tel.record_timeout(STAGE_NETWORK)
    tel.record_success(StageTimings(upload_ms=5.0))

    tel.reset()

    assert tel.snapshot() == ClientTelemetry().snapshot()


def test_concurrent_records_do_not_lose_counts() -> None:
    tel = ClientTelemetry()
    n_threads, per_thread = 8, 50

    def _worker() -> None:
        for _ in range(per_thread):
            tel.record_attempt()
            tel.record_timeout(STAGE_NETWORK)

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snap = tel.snapshot()

    assert snap["attempts"] == n_threads * per_thread
    assert snap["timeouts"] == n_threads * per_thread
    assert snap["failure_stages"][STAGE_NETWORK] == n_threads * per_thread
