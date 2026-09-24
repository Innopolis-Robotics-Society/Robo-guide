"""Финал ASR не ждёт партиал: у onnxruntime-бэкенда свой поток для финалов."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from guide_robot_voice.asr_node import AsrNode, _DecodeJob
from guide_robot_voice.lib.asr_model import AsrResult, GigaAmCtc, OrtGigaAmCtc


class _SlowPartialAsr(OrtGigaAmCtc):
    """Партиал декодируется 0.5 с, финал -- мгновенно."""

    def __init__(self) -> None:
        super().__init__("model.onnx", "tokens.txt")

    def decode(self, pcm: np.ndarray) -> AsrResult:
        time.sleep(0.5 if pcm.size == 1 else 0.0)
        return AsrResult(text="", confidence=-1.0)


def _node(asr: object) -> tuple[AsrNode, list[str]]:
    node = AsrNode.__new__(AsrNode)
    node._lock = threading.Lock()
    node._asr = asr
    node._decode_stop = threading.Event()
    node._decode_worker = None
    node._final_worker = None
    node._utterance_open = True
    node._utterance_id = 1
    done: list[str] = []
    node._run_decode = lambda job: done.append(job.kind)
    node.get_logger = lambda: SimpleNamespace(error=lambda _msg: None)
    return node, done


def _job(kind: str, size: int) -> _DecodeJob:
    return _DecodeJob(kind, np.zeros(size, dtype=np.int16), 1, 0.0, 0, size, 0.0, "s", 0, size)


def test_onnxruntime_final_gets_its_own_worker() -> None:
    node, _ = _node(_SlowPartialAsr())
    node._start_decode_worker()
    try:
        assert node._final_jobs is not node._decode_jobs
        assert node._final_worker is not None and node._final_worker.is_alive()
    finally:
        node._stop_decode_worker()
    assert node._decode_worker is None and node._final_worker is None


def test_sherpa_keeps_single_queue() -> None:
    node, _ = _node(GigaAmCtc("model.onnx", "tokens.txt"))
    node._start_decode_worker()
    try:
        assert node._final_jobs is node._decode_jobs
        assert node._final_worker is None
    finally:
        node._stop_decode_worker()


def test_final_is_not_blocked_by_running_partial() -> None:
    node, done = _node(_SlowPartialAsr())
    started = threading.Event()

    def run(job: _DecodeJob) -> None:
        if job.kind == "partial":
            started.set()
            time.sleep(0.5)
        done.append(job.kind)

    node._run_decode = run
    node._start_decode_worker()
    try:
        node._decode_jobs.put(_job("partial", 1))
        assert started.wait(1.0)
        node._final_jobs.put(_job("final", 16000))
        deadline = time.monotonic() + 0.3
        while "final" not in done and time.monotonic() < deadline:
            time.sleep(0.01)
        assert done[:1] == ["final"]  # финал готов, пока партиал ещё идёт
    finally:
        node._stop_decode_worker()


def test_stale_partial_is_skipped() -> None:
    node, done = _node(_SlowPartialAsr())
    node._utterance_open = False
    node._start_decode_worker()
    try:
        node._decode_jobs.put(_job("partial", 1))
        time.sleep(0.2)
        assert done == []
    finally:
        node._stop_decode_worker()
