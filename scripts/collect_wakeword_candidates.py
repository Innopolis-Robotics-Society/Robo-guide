#!/usr/bin/env python3
"""Сохранять звук каждого KWS-кандидата и раскладывать его по исходу проверки.

Запуск внутри контейнера, рядом с работающим desk (ничего в стеке не меняет,
только слушает топики):
  source /opt/ros/humble/setup.bash && source /home/fabian/ros2_ws/install/setup.bash
  python3 /home/fabian/ros2_ws/src/scripts/collect_wakeword_candidates.py
Остановка — Ctrl+C; в конце печатается сводка.

На каждый /voice/wakeword_candidate берётся 3 с до end_sample и 1 с после
из /audio/mic (тот же обработанный канал, что слышит модель). Исход:
  negative     — manager допустил кандидата, ASR не нашёл имени
                 (UtteranceEvent status=wake_not_confirmed). Пишется сразу
                 в raw/negative/train/<сессия>/ формата record_wakeword_dataset.py,
                 поэтому prepare_wakeword_dataset.py подхватит его как negative
                 и ещё раз проверит окна на похожие на имя слова.
  confirmed    — ASR подтвердил имя (wakeword_only или final). Не обязательно
                 настоящее обращение: только на просмотр.
  not_admitted — manager не допустил кандидата (речь робота, открытое окно,
                 повтор). Без разметки, только на просмотр.
  unresolved   — допущен, но исход не пришёл за --outcome-timeout.
Всё, кроме negative, лежит в robot_candidates/<сессия>/<исход>/; общий
manifest.jsonl этой сессии описывает все кандидаты, включая negative.

Отказ проверки — не доказательство отсутствия имени: GigaAM иногда не
узнаёт искажённое имя. Перед обучением прогоните prepare_wakeword_dataset.py
и выборочно прослушайте negative этой сессии.
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

RATE = 16_000
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "datasets/wakeword/firaya_v1"
CONFIRMED_STATUSES = frozenset({"wakeword_only", "final"})


@dataclass
class Candidate:
    """KWS-кандидат, ожидающий звука после end_sample и исхода проверки."""

    number: int
    session: str
    end_sample: int
    score: float
    model_sha256: str
    received_at: float
    tts_active: bool
    utterance_id: int = 0
    outcome: str = ""
    status: str = ""
    asr_text: str = ""
    extra: dict = field(default_factory=dict)


class AudioRing:
    """Последние max_seconds PCM по абсолютным номерам сэмплов одной capture-сессии."""

    def __init__(self, max_seconds: float = 30.0) -> None:
        """Ограничить память: кандидатов старше этого окна уже не сохранить."""
        self.max_samples = int(max_seconds * RATE)
        self.session = ""
        self.blocks: deque[tuple[int, np.ndarray]] = deque()
        self.gaps = 0

    @property
    def end(self) -> int:
        """Номер сэмпла сразу после последнего полученного."""
        if not self.blocks:
            return 0
        first, data = self.blocks[-1]
        return first + data.size

    def push(self, session: str, first: int, data: np.ndarray) -> None:
        """Новая сессия захвата обнуляет историю: номера сэмплов несравнимы."""
        if session != self.session:
            self.session = session
            self.blocks.clear()
        elif self.blocks and first != self.end:
            self.gaps += 1
        self.blocks.append((first, data))
        while self.blocks:
            first, data = self.blocks[0]
            if self.end - first - data.size <= self.max_samples:
                break
            self.blocks.popleft()

    def extract(self, session: str, start: int, stop: int) -> np.ndarray | None:
        """Вернуть [start, stop) с нулями на разрывах; None, если звука ещё нет."""
        if session != self.session or self.end < stop:
            return None
        out = np.zeros(stop - start, dtype=np.int16)
        for first, data in self.blocks:
            lo, hi = max(start, first), min(stop, first + data.size)
            if lo < hi:
                out[lo - start : hi - start] = data[lo - first : hi - first]
        return out


class CandidateTracker:
    """Связать кандидата с решением manager и итогом ASR без ROS."""

    def __init__(self, admit_timeout: float, outcome_timeout: float) -> None:
        """Таймауты в секундах настенного времени от прихода кандидата."""
        self.admit_timeout = admit_timeout
        self.outcome_timeout = outcome_timeout
        self.pending: list[Candidate] = []
        self.count = 0
        self.texts: dict[int, str] = {}

    def add(self, **values: object) -> Candidate:
        """Зарегистрировать новый кандидат."""
        self.count += 1
        candidate = Candidate(number=self.count, **values)
        self.pending.append(candidate)
        return candidate

    def on_admit(self, session: str, onset_sample: int, utterance_id: int) -> None:
        """manager открывает wake-реплику ровно с end_sample кандидата."""
        for candidate in self.pending:
            if candidate.session == session and candidate.end_sample == onset_sample:
                candidate.utterance_id = utterance_id

    def on_text(self, utterance_id: int, text: str) -> None:
        """Последний партиал/финал реплики — подсказка для просмотра."""
        if utterance_id and text:
            self.texts[utterance_id] = text
            while len(self.texts) > 200:
                self.texts.pop(next(iter(self.texts)))

    def on_result(self, utterance_id: int, status: str) -> None:
        """FINAL/DISCARDED wake-реплики задаёт исход кандидата."""
        for candidate in self.pending:
            if candidate.utterance_id == utterance_id and utterance_id and not candidate.outcome:
                candidate.status = status
                if status == "wake_not_confirmed":
                    candidate.outcome = "negative"
                elif status in CONFIRMED_STATUSES:
                    candidate.outcome = "confirmed"
                else:
                    candidate.outcome = "unresolved"

    def expire(self, now: float) -> None:
        """Без допуска или без итога кандидат не размечается автоматически."""
        for candidate in self.pending:
            if candidate.outcome:
                continue
            age = now - candidate.received_at
            if not candidate.utterance_id and age > self.admit_timeout:
                candidate.outcome = "not_admitted"
            elif candidate.utterance_id and age > self.outcome_timeout:
                candidate.outcome = "unresolved"

    def ready(self) -> list[Candidate]:
        """Снять кандидатов с известным исходом; звук проверяет вызывающий."""
        done = [candidate for candidate in self.pending if candidate.outcome]
        for candidate in done:
            candidate.asr_text = self.texts.get(candidate.utterance_id, "")
        return done


def session_name(now: datetime) -> str:
    """Не пересекается с dNN ручных записей при переносе на ПК."""
    return now.strftime("kws%Y%m%dT%H%M%SZ")


def write_wav(path: Path, pcm: np.ndarray) -> None:
    """Mono PCM16 16 кГц, как у record_wakeword_dataset.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        stream.writeframes(pcm.astype("<i2").tobytes())


def metrics(pcm: np.ndarray) -> dict:
    """Те же поля качества, что пишет ручной рекордер."""
    values = pcm.astype(np.float64)
    rms = float(np.sqrt(np.mean(values**2))) if values.size else 0.0
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    return {
        "rms_dbfs": 20 * np.log10(max(rms, 1e-9) / 32768),
        "peak_dbfs": 20 * np.log10(max(peak, 1e-9) / 32768),
        "clipped_samples": int(np.sum(np.abs(pcm.astype(np.int32)) >= 32767)),
    }


class CandidateWriter:
    """Раскладка файлов и manifest-записей одной сессии сбора."""

    def __init__(self, root: Path, session: str) -> None:
        """root — каталог датасета (firaya_v1), session — имя сессии сбора."""
        self.root = root
        self.session = session
        self.raw_dir = root / "raw/negative/train" / session
        self.review_dir = root / "robot_candidates" / session
        self.counts: dict[str, int] = {}

    def save(self, candidate: Candidate, pcm: np.ndarray, before: float, after: float) -> Path:
        """Записать WAV и строки manifest; negative — в формате raw-датасета."""
        self.counts[candidate.outcome] = self.counts.get(candidate.outcome, 0) + 1
        created = datetime.now(timezone.utc).isoformat()
        quality = metrics(pcm)
        details = {
            "kws_score": candidate.score,
            "end_sample": candidate.end_sample,
            "device_session_id": candidate.session,
            "model_sha256": candidate.model_sha256,
            "tts_active": candidate.tts_active,
            "utterance_id": candidate.utterance_id,
            "asr_status": candidate.status,
            "asr_text_hint": candidate.asr_text,
            "seconds_before_trigger": before,
            "seconds_after_trigger": after,
        }
        if candidate.outcome == "negative":
            take = self.counts["negative"]
            path = self.raw_dir / f"kws_false_trigger_{take:03d}.wav"
            write_wav(path, pcm)
            entry = {
                "created_at_utc": created,
                "plan": "kws_false_trigger_v1",
                "kind": "negative",
                "split": "train",
                "speaker": None,
                "session": self.session,
                "condition": "kws_false_trigger",
                "take": take,
                "topic": "/audio/mic",
                "file": str(path.relative_to(self.root / "raw")),
                "status": "accepted",
                "sample_rate": RATE,
                "channels": 1,
                "duration_seconds": pcm.size / RATE,
                "gaps": 0,
                "channel_metrics": [{"channel": 0, **quality}],
                "channel": 0,
                **quality,
                **details,
            }
            self._append(self.raw_dir / "manifest.jsonl", entry)
        else:
            path = self.review_dir / candidate.outcome / f"cand_{candidate.number:04d}.wav"
            write_wav(path, pcm)
        self._append(
            self.review_dir / "manifest.jsonl",
            {
                "created_at_utc": created,
                "number": candidate.number,
                "outcome": candidate.outcome,
                "file": str(path.relative_to(self.root)),
                **quality,
                **details,
            },
        )
        return path

    @staticmethod
    def _append(path: Path, entry: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run(arguments: argparse.Namespace) -> int:
    """ROS-часть: только подписки, никаких публикаций в стек."""
    import rclpy
    from guide_robot_msgs.msg import (
        AudioChunk,
        SpeakingStatus,
        Transcript,
        UtteranceControl,
        UtteranceEvent,
        WakewordCandidate,
    )
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rclpy.signals import SignalHandlerOptions

    from guide_robot_voice.lib.qos import (
        QOS_ASR_PARTIAL,
        QOS_ASR_TRANSCRIPT,
        QOS_UTTERANCE_CONTROL,
        QOS_UTTERANCE_EVENT,
        QOS_VOICE_SPEAKING,
        QOS_WAKEWORD,
    )

    before = int(arguments.before * RATE)
    after = int(arguments.after * RATE)
    ring = AudioRing(max(30.0, arguments.outcome_timeout + arguments.before + 5))
    tracker = CandidateTracker(arguments.admit_timeout, arguments.outcome_timeout)
    writer = CandidateWriter(arguments.root, session_name(datetime.now(timezone.utc)))
    speaking = {"value": False}

    # Ctrl+C -- обычный KeyboardInterrupt: обработчик rclpy рвёт контекст до сводки.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Node("wakeword_candidate_collector")
    log = node.get_logger()

    def on_audio(msg: AudioChunk) -> None:
        data = np.asarray(msg.data, dtype=np.int16)
        if msg.channels != 1:
            data = data.reshape(-1, msg.channels)[:, 0]
        ring.push(msg.device_session_id, int(msg.first_sample), data)

    def on_candidate(msg: WakewordCandidate) -> None:
        candidate = tracker.add(
            session=msg.device_session_id,
            end_sample=int(msg.end_sample),
            score=float(msg.confidence),
            model_sha256=msg.model_sha256,
            received_at=time.monotonic(),
            tts_active=speaking["value"],
        )
        log.info(
            f"кандидат #{candidate.number}: score={candidate.score:.3f} "
            f"tts={candidate.tts_active} end_sample={candidate.end_sample}"
        )

    def on_control(msg: UtteranceControl) -> None:
        if msg.reason == "acoustic_wakeword" and msg.decision == UtteranceControl.DECISION_ADMIT:
            tracker.on_admit(msg.device_session_id, int(msg.onset_sample), int(msg.utterance_id))

    def on_event(msg: UtteranceEvent) -> None:
        if msg.event in (UtteranceEvent.EVENT_FINAL, UtteranceEvent.EVENT_DISCARDED):
            tracker.on_result(int(msg.utterance_id), msg.status)

    def on_text(msg: Transcript) -> None:
        tracker.on_text(int(msg.utterance_id), msg.text)

    def on_speaking(msg: SpeakingStatus) -> None:
        speaking["value"] = bool(msg.speaking)

    audio_qos = QoSProfile(depth=200, reliability=ReliabilityPolicy.BEST_EFFORT)
    for message, topic, callback, qos in (
        (AudioChunk, "/audio/mic", on_audio, audio_qos),
        (WakewordCandidate, "/voice/wakeword_candidate", on_candidate, QOS_WAKEWORD),
        (UtteranceControl, "/voice/input_control", on_control, QOS_UTTERANCE_CONTROL),
        (UtteranceEvent, "/voice/utterance_event", on_event, QOS_UTTERANCE_EVENT),
        (Transcript, "/voice/kws_partial", on_text, QOS_ASR_PARTIAL),
        (Transcript, "/asr/partial", on_text, QOS_ASR_PARTIAL),
        (Transcript, "/asr/transcript", on_text, QOS_ASR_TRANSCRIPT),
        (SpeakingStatus, "/voice/speaking", on_speaking, QOS_VOICE_SPEAKING),
    ):
        node.create_subscription(message, topic, callback, qos)

    print(
        f"Сбор кандидатов: negative -> {writer.raw_dir}\n"
        f"остальные -> {writer.review_dir}\nCtrl+C — остановить.",
        flush=True,
    )
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
            tracker.expire(time.monotonic())
            for candidate in tracker.ready():
                end = candidate.end_sample
                pcm = ring.extract(candidate.session, end - before, end + after)
                if pcm is None:
                    if ring.session != candidate.session:
                        tracker.pending.remove(candidate)
                        log.warning(f"кандидат #{candidate.number}: сессия захвата сменилась")
                    continue
                tracker.pending.remove(candidate)
                path = writer.save(candidate, pcm, arguments.before, arguments.after)
                hint = f" текст={candidate.asr_text!r}" if candidate.asr_text else ""
                log.info(f"кандидат #{candidate.number} -> {candidate.outcome}{hint}: {path}")
    except KeyboardInterrupt:
        pass
    finally:
        summary = writer.counts or "кандидатов не было"
        print(f"\nИтог сессии {writer.session}: {summary}", flush=True)
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Разобрать аргументы и запустить сбор."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="каталог firaya_v1")
    parser.add_argument("--before", type=float, default=3.0, help="секунд звука до срабатывания")
    parser.add_argument("--after", type=float, default=1.0, help="секунд звука после срабатывания")
    parser.add_argument("--admit-timeout", type=float, default=1.5)
    parser.add_argument("--outcome-timeout", type=float, default=25.0)
    arguments = parser.parse_args(argv)
    return run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
