#!/usr/bin/env python3
"""Живые KWS-тесты по два раза: Enter, WAV и результат.

В контейнере с запущенным /audio/mic и подключённым install/setup.bash.
Подсказки только визуальные: звуковой сигнал не загрязняет запись.
Разметка по времени подсказок предварительная, не ASR и не проверка произношения.
--dry-run не требует ROS/ONNX и ничего не создаёт. Ctrl+C сохраняет неполный WAV.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.metadata
import io
import json
import math
import os
import sys
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .predict_wakeword_wav import streaming_scores
    from .train_wakeword_trial import DATA, FRAME, RATE, event_indices, read_wav
except ImportError:
    from predict_wakeword_wav import streaming_scores
    from train_wakeword_trial import DATA, FRAME, RATE, event_indices, read_wav

PARAGRAPH = (
    "Сегодня мы проверяем микрофон и модель робота. В помещении разговаривают люди. "
    "Рядом с картиной находится выход. Фирма открыла филиал. "
    "В эфире обсуждали фильм и историю художника."
)


@dataclass(frozen=True)
class Scenario:
    """Один сценарий с предполагаемыми метками, а не подтверждённой транскрипцией."""

    number: int
    title: str
    utterances: tuple[tuple[str, bool | int], ...]
    interval: float = 3
    continuous: bool = False
    seconds: float = 20
    start_seconds: float = 2

    @property
    def expected(self):
        """Вернуть запланированное количество произнесений имени."""
        return sum(label for _, label in self.utterances)

    def cues(self, duration):
        """Разнести слова, оставляя время произнести их и получить KWS-ответ."""
        return [
            {
                "text": text,
                "contains_wakeword": bool(label),
                **({"expected_events": label} if type(label) is int else {}),
                "planned_seconds": self.start_seconds + index * self.interval,
                "window_end_seconds": duration
                if self.continuous
                else min(self.start_seconds + (index + 1) * self.interval, duration),
            }
            for index, (text, label) in enumerate(self.utterances)
        ]


TESTS = (
    Scenario(1, "Только имя", (("ФирАя", True),) * 5),
    Scenario(
        2,
        "Слова на Ф",
        tuple(
            (word, False) for word in ("Феррари", "филлер", "фонвизин", "фидер", "фильтр", "фирма")
        ),
    ),
    Scenario(
        3,
        "Похожие звучания",
        tuple((word, False) for word in ("Фарида", "Фиона", "фурия", "Фира", "Фая", "Рая")),
    ),
    Scenario(
        4,
        "Обычные слова",
        tuple(
            (word, False) for word in ("Модель", "робот", "картина", "выход", "хорошо", "спасибо")
        ),
    ),
    Scenario(
        5,
        "Смешанный",
        (
            ("Феррари", False),
            ("ФирАя", True),
            ("филлер", False),
            ("ФирАя", True),
            ("модель", False),
            ("ФирАя", True),
        ),
    ),
    Scenario(
        6,
        "Имя внутри фразы",
        tuple(
            (text, True)
            for text in ("ФирАя, расскажи про картину", "Привет, ФирАя", "Скажи, ФирАя, где выход")
        ),
        interval=6,
    ),
    Scenario(7, "Связная речь без имени", ((PARAGRAPH, False),), continuous=True),
)

# Preserve the old seven-test protocol for explicit --suite classic and resume.
# Every phrase has at most one target: cue labels remain unambiguous.
EXTENDED_TESTS = TESTS + (
    Scenario(
        8,
        "Связная речь: имя в начале",
        tuple(
            (text, True)
            for text in (
                "ФирАя, мы пришли в музей, расскажи про эту картину",
                "ФирАя, здесь много людей, помоги нам найти выход",
                "ФирАя, я хочу узнать историю этого художника и его работ",
            )
        ),
        interval=6,
    ),
    Scenario(
        9,
        "Связная речь: имя в середине",
        tuple(
            (text, True)
            for text in (
                "Мы пришли в музей, ФирАя, расскажи про эту картину",
                "Здесь сегодня много людей, ФирАя, помоги нам найти выход",
                "Я смотрю на картину, ФирАя, кто её написал",
            )
        ),
        interval=6,
    ),
    Scenario(
        10,
        "Связная речь: имя в конце",
        tuple(
            (text, True)
            for text in (
                "Мы пришли в музей и хотим узнать его историю, ФирАя",
                "Расскажи нам про художника и про эту картину, ФирАя",
                "Здесь сегодня много людей, помоги нам найти выход, ФирАя",
            )
        ),
        interval=6,
    ),
    Scenario(
        11,
        "Та же связная речь без имени",
        tuple(
            (text, False)
            for text in (
                "Мы пришли в музей, расскажи нам про эту картину",
                "Здесь сегодня много людей, помоги нам найти выход",
                "Я хочу узнать историю этого художника и его работ",
            )
        ),
        interval=6,
    ),
)

# New text, not the phrases used to generate the continuous training corpus.
# One target per cue keeps count/slot assessment compatible with older reports.
# Longer windows are essential: reading should not become a race with the timer.
SPEECH_TESTS = TESTS + (
    Scenario(
        8,
        "Длинные фразы: имя в начале",
        tuple(
            (text, True)
            for text in (
                "ФирАя мы только что вошли в этот зал и пока не понимаем какие работы здесь "
                "представлены расскажи пожалуйста с чего начать осмотр",
                "ФирАя наша группа немного отстала от экскурсовода и теперь мы хотим "
                "самостоятельно разобраться почему эта картина считается такой важной",
                "ФирАя после прогулки по выставке мы собираемся встретиться с друзьями возле "
                "входа подскажи пожалуйста как туда пройти из этого зала",
            )
        ),
        interval=12,
        seconds=40,
    ),
    Scenario(
        9,
        "Длинные фразы: имя в середине",
        tuple(
            (text, True)
            for text in (
                "Мы остановились возле большой картины и теперь хотим узнать ФирАя в каком году "
                "художник закончил эту работу и где он тогда жил",
                "За окном начался дождь поэтому мы решили остаться на выставке подольше ФирАя "
                "расскажи какие ещё интересные работы можно посмотреть в соседнем помещении",
                "Мы долго обсуждали цвета и детали этого портрета но так и не договорились ФирАя "
                "объясни пожалуйста что хотел показать автор",
            )
        ),
        interval=12,
        seconds=40,
    ),
    Scenario(
        10,
        "Длинные фразы: имя в конце",
        tuple(
            (text, True)
            for text in (
                "Мы уже посмотрели несколько картин в соседнем зале и теперь хотим понять чем "
                "эта работа отличается от остальных объясни пожалуйста ФирАя",
                "В нашей группе есть человек который впервые пришёл в музей и пока ничего не "
                "знает об этом художнике расскажи ему самое интересное ФирАя",
                "После экскурсии мы собираемся немного отдохнуть но сначала хотим найти гардероб "
                "и забрать оставленные там вещи помоги нам пожалуйста ФирАя",
            )
        ),
        interval=12,
        seconds=40,
    ),
    Scenario(
        11,
        "Длинные фразы: без имени",
        tuple(
            (text, False)
            for text in (
                "Мы остановились возле большой картины и теперь хотим узнать в каком году "
                "художник закончил эту работу и где он тогда жил",
                "За окном начался дождь поэтому мы решили остаться на выставке подольше расскажи "
                "какие ещё интересные работы можно посмотреть в соседнем помещении",
                "Мы долго обсуждали цвета и детали этого портрета но так и не договорились "
                "объясни пожалуйста что хотел показать автор",
            )
        ),
        interval=12,
        seconds=40,
    ),
    Scenario(
        12,
        "Абзац: одно имя среди разговора",
        (
            (
                "Сегодня мы встретились с друзьями возле музея и решили посмотреть новую "
                "выставку. "
                "Сначала оставили куртки в гардеробе затем прошли через большой коридор и "
                "остановились у первой картины. "
                "Один из нас уже видел эту работу в книге но не запомнил имя автора поэтому ФирАя "
                "расскажи нам об этой картине. "
                "После этого мы спокойно пойдём дальше и посмотрим остальные работы в соседнем "
                "зале.",
                True,
            ),
        ),
        continuous=True,
        seconds=45,
    ),
    Scenario(
        13,
        "Абзац: имя после долгой речи",
        (
            (
                "Вчера мы собирались пойти на прогулку но погода неожиданно испортилась и "
                "пришлось изменить планы. "
                "Тогда друзья предложили встретиться в музее где сейчас проходит интересная "
                "выставка. "
                "Мы приехали немного раньше купили билеты и успели посмотреть первый зал. "
                "Здесь оказалось много портретов и пейзажей а возле одной картины мы задержались "
                "особенно надолго потому что хотим узнать её историю ФирАя.",
                True,
            ),
        ),
        continuous=True,
        seconds=45,
    ),
    Scenario(
        14,
        "Абзац: похожие слова без имени",
        (
            (
                "Вчера Фарида и Фиона обсуждали новый фильм пока Рая выбирала фотографии для "
                "выставки. "
                "Потом разговор перешёл на машины и кто-то вспомнил красную Феррари которую видел "
                "возле дома. "
                "В эфире по радио рассказывали про фирму открывшую филиал в соседнем городе. "
                "Мы проверили фильтр в микрофоне и вернулись к разговору о картинах художниках и "
                "планах на выходные.",
                False,
            ),
        ),
        continuous=True,
        seconds=45,
    ),
    Scenario(
        15,
        "Абзац: тот же разговор без имени",
        (
            (
                "Сегодня мы встретились с друзьями возле музея и решили посмотреть новую "
                "выставку. "
                "Сначала оставили куртки в гардеробе затем прошли через большой коридор и "
                "остановились у первой картины. "
                "Один из нас уже видел эту работу в книге но не запомнил имя автора поэтому "
                "расскажи нам об этой картине. "
                "После этого мы спокойно пойдём дальше и посмотрим остальные работы в соседнем "
                "зале.",
                False,
            ),
        ),
        continuous=True,
        seconds=45,
    ),
)

LONG_PARAGRAPHS = (
    "ФирАя сегодня мы решили спокойно пройти по выставке и поговорить о том что видим вокруг. "
    "У входа встретились Фарида и Фиона а Рая уже ждала нас возле гардероба. "
    "По дороге сюда мы заметили красную Феррари и долго обсуждали почему такие машины редко "
    "встречаются на наших улицах. "
    "Затем разговор перешёл на фотографии которые друзья сделали во время прошлой поездки.",
    "В первом зале было несколько больших портретов и маленькая картина с видом на реку. "
    "Мы остановились возле неё потому что один из посетителей вспомнил похожий пейзаж из детства. "
    "Пока остальные обсуждали цвета я подумал ФирАя расскажи пожалуйста где художник мог увидеть "
    "такое место. "
    "Фарида предложила поискать ответ в описании работы а Фиона начала читать табличку рядом с "
    "рамой.",
    "После этого мы прошли в соседнее помещение где было заметно тише. "
    "Кто-то вспомнил фильм о художниках а кто-то снова заговорил про Феррари которую мы видели "
    "утром. "
    "В эфире по радио недавно обсуждали новую фирму её филиал и фотографии старого города. "
    "Нам захотелось разобраться как менялись эти улицы за последние сто лет поэтому помоги нам "
    "найти подходящую картину ФирАя.",
    "Дальше экскурсовод показал несколько набросков и объяснил как автор готовился к большой "
    "работе. "
    "Рая заметила необычную деталь на одном рисунке а Фарида попросила остальных подойти ближе. "
    "Мы ещё немного поговорили о композиции и освещении а затем ФирАя объясни пожалуйста почему "
    "художник оставил этот участок почти пустым. "
    "Фиона сказала что хотела бы попробовать нарисовать похожий пейзаж дома.",
    "ФирАя теперь мы собираемся вернуться в первый зал чтобы ещё раз посмотреть на понравившийся "
    "портрет. "
    "По дороге друзья обсуждают фильтр для фотоаппарата новый фильм и яркие фотографии с "
    "выставки. "
    "В разговоре снова появляются Феррари Фарида Фиона и Рая но мы продолжаем спокойно идти по "
    "коридору и смотреть на картины.",
    "В конце прогулки мы решили встретиться у входа забрать вещи и обсудить дальнейшие планы. "
    "Одни хотят зайти в кафе другие предлагают прогуляться по набережной если дождь закончится. "
    "Мы ещё раз вспомнили интересные работы обменялись фотографиями и договорились прийти сюда в "
    "следующий раз. "
    "А сейчас подскажи пожалуйста где находится ближайший выход ФирАя.",
)
LONG_TEXT = "\n\n".join(LONG_PARAGRAPHS)
LONG_TESTS = (
    Scenario(
        1,
        "Длинная связная речь: 6 имён и похожие слова",
        ((LONG_TEXT, 6),),
        continuous=True,
        seconds=240,
        start_seconds=5,
    ),
    Scenario(
        2,
        "Контроль: тот же текст без имени",
        ((LONG_TEXT.replace("ФирАя", "Фарида"), False),),
        continuous=True,
        seconds=240,
        start_seconds=5,
    ),
)
SUITES = {"classic": TESTS, "extended": EXTENDED_TESTS, "speech": SPEECH_TESTS, "long": LONG_TESTS}


def encode(value):
    """JSON в UTF-8 без потери русских подсказок."""
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def write_atomic(path: Path, data: bytes, *, replace=False):
    """Атомарно сохранить отчёт; новые WAV и модель никогда не перезаписывать."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".kws-test-", delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def wav_bytes(pcm, sample_rate, channels):
    """Упаковать исходный PCM без нормализации, обрезки и подавления шума."""
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((channels, 2, sample_rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return stream.getvalue()


def capture(topic: str, duration: float, planned: list[dict]):
    """Записывать по аудиочасам и сохранять реальные моменты вывода подсказок."""
    try:
        import rclpy

        if __package__:
            from .xvf3800_ros_channel_probe import ChannelProbe, analyse_interleaved
        else:
            from xvf3800_ros_channel_probe import ChannelProbe, analyse_interleaved
    except ImportError as error:
        raise RuntimeError(
            "Запускайте внутри ROS-контейнера после source install/setup.bash"
        ) from error
    rclpy.init(args=[])
    node = ChannelProbe(topic)
    cues, interrupted = [], False
    try:
        print("Подключаюсь к микрофону. Пока молчи…", flush=True)
        deadline = time.monotonic() + 5
        while not node.blocks and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        if not node.blocks:
            raise RuntimeError(f"Нет данных из {topic}. Запустите xvf3800_audio.launch.py")
        if (node.sample_rate, node.channels) != (RATE, 1):
            raise RuntimeError("Для KWS нужен mono PCM16 @16k из /audio/mic")
        # Drain discovery/warm-up audio; recording and cue times have one origin.
        node.blocks.clear()
        node.gaps = 0
        frames, seen, stopped = 0, 0, set()
        deadline = time.monotonic() + duration + 5
        print(
            "ЗАПИСЬ НАЧАЛАСЬ. Говори только после >>> ГОВОРИ. Между подсказками молчи.", flush=True
        )
        try:
            while rclpy.ok() and frames / node.sample_rate < duration:
                if time.monotonic() > deadline:
                    break
                rclpy.spin_once(node, timeout_sec=0.05)
                frames += sum(block.shape[0] for block in node.blocks[seen:])
                seen = len(node.blocks)
                elapsed = frames / node.sample_rate
                while (
                    len(cues) < len(planned) and elapsed >= planned[len(cues)]["planned_seconds"]
                ):
                    cue = {**planned[len(cues)], "actual_seconds": elapsed}
                    cues.append(cue)
                    print(f"\n>>> ГОВОРИ ({elapsed:.1f} с): {cue['text']}\n", flush=True)
                # Leave one second for silence/detector latency before the next
                # slot. Do not emit an audible beep into the microphone.
                for index, cue in enumerate(cues):
                    if index not in stopped and elapsed >= cue["window_end_seconds"] - 1:
                        cue["stop_actual_seconds"] = elapsed
                        stopped.add(index)
                        print(
                            "\n>>> МОЛЧИ. Жди следующую подсказку; не повторяй сказанное.\n",
                            flush=True,
                        )
        except KeyboardInterrupt:
            interrupted = True
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    if not node.blocks:
        raise RuntimeError("После начала записи аудиоданные не поступили")
    samples = np.concatenate(node.blocks)
    metrics = {
        "sample_rate": node.sample_rate,
        "channels": node.channels,
        "duration_seconds": len(samples) / node.sample_rate,
        "gaps": node.gaps,
        "channel_metrics": analyse_interleaved(samples),
        "interrupted": interrupted,
        "cues": cues,
    }
    return samples.astype("<i2", copy=False).tobytes(), metrics


def load_runtime(model_path: Path, config: dict, backbone: Path):
    """Загрузить неизменную модель, используя уже изолированные зависимости проекта."""
    dependencies = DATA / "experiments/_python_deps"
    if dependencies.is_dir():
        sys.path.insert(0, str(dependencies))
    if (
        type(config.get("consecutive_frames")) is not int
        or not 1 <= config["consecutive_frames"] <= 20
        or not 0 <= config["threshold"] <= 1
    ):
        raise ValueError("Нужны 1..20 кадров подтверждения и порог в [0, 1]")
    if config.get("feature_kind") == "logmel80":
        try:
            from .wakeword_mel import MelRuntime
        except ImportError:
            from wakeword_mel import MelRuntime
        return MelRuntime(model_path, config)
    from openwakeword.model import Model

    return Model(
        wakeword_models=[str(model_path)],
        inference_framework="onnx",
        melspec_model_path=str(backbone / "melspectrogram.onnx"),
        embedding_model_path=str(backbone / "embedding_model.onnx"),
        ncpu=1,
    )


def assess_events(events: list[float], cues: list[dict]):
    """Предварительно сопоставить активации со слотами; не выдавать их за ASR-разметку."""
    slots, assigned = [], set()
    for index, cue in enumerate(cues):
        start = cue["actual_seconds"]
        end = (
            cues[index + 1]["actual_seconds"]
            if index + 1 < len(cues)
            else cue["window_end_seconds"]
        )
        matches = [i for i, event in enumerate(events) if start <= event < end]
        assigned.update(matches)
        expected = cue.get("expected_events", int(cue["contains_wakeword"]))
        slots.append(
            {
                **cue,
                "event_seconds": [events[i] for i in matches],
                "estimated_hits_in_slot": min(expected, len(matches)),
                "estimated_misses_in_slot": max(0, expected - len(matches)),
                "estimated_hit": cue["contains_wakeword"] and bool(matches),
                "estimated_miss": len(matches) < expected,
                "estimated_extra": max(0, len(matches) - expected),
            }
        )
    outside = [event for i, event in enumerate(events) if i not in assigned]
    return {
        "slots": slots,
        "outside_slot_events": outside,
        "estimated_hits": sum(slot["estimated_hits_in_slot"] for slot in slots),
        "estimated_misses": sum(slot["estimated_misses_in_slot"] for slot in slots),
        "estimated_extra_events": sum(slot["estimated_extra"] for slot in slots) + len(outside),
        "label_verified": False,
        "matching_mode": "count_only_multitarget"
        if any(cue.get("expected_events", 0) > 1 for cue in cues)
        else "cue_slots",
        "warning": "Это сопоставление по подсказкам, не подтверждённое распознавание слов.",
    }


def infer_record(runtime, config, path: Path, record: dict):
    """Сохранить каждый 80-мс score и времена активаций; без искусственного хвоста."""
    audio = read_wav(path)
    scores = streaming_scores(runtime, "firaya_trial", audio)
    events = [
        min((i + 1) * FRAME / RATE, len(audio) / RATE)
        for i in event_indices(
            scores, config["threshold"], config["cooldown_seconds"], config["consecutive_frames"]
        )
    ]
    valid = (
        record["capture"]["gaps"] == 0
        and not record["capture"]["interrupted"]
        and len(record["capture"]["cues"]) == len(record["planned_cues"])
        and record["capture"]["duration_seconds"] >= record["requested_seconds"] - 0.2
        and all(
            c["actual_seconds"] - c["planned_seconds"] < 0.5 for c in record["capture"]["cues"]
        )
    )
    result = {
        **record,
        "status": "complete",
        "audio_protocol_valid": valid,
        "observed_events": len(events),
        "event_seconds": events,
        "max_score": float(max(scores, default=0)),
        "count_matches": len(events) == record["expected_events"],
        "assessment": assess_events(events, record["capture"]["cues"]),
    }
    curve = {
        "frame_ms": 80,
        "threshold": config["threshold"],
        "frame_end_seconds": [
            min((i + 1) * FRAME / RATE, len(audio) / RATE) for i in range(len(scores))
        ],
        "scores": scores.tolist(),
    }
    return result, curve


def update_reports(output: Path):
    """Обновить JSON, CSV и HTML после каждого дубля, даже если сеанс не завершён."""
    import csv

    rows = [
        json.loads(path.read_text())
        for path in sorted((output / "takes").glob("*.json"))
        if not path.name.endswith("_scores.json")
    ]
    complete = [row for row in rows if row["status"] == "complete"]
    session = json.loads((output / "session.json").read_text())
    planned_keys = {
        f"test_{task['test']:02d}_repeat_{task['repeat']:02d}" for task in session["protocol"]
    }
    completed_keys = {row["key"] for row in complete} & planned_keys
    planned_count = len(planned_keys)
    summary = {
        "completed_takes": len(completed_keys),
        "planned_takes": planned_count,
        "complete": completed_keys == planned_keys,
        "label_policy": "cue_based_unverified",
        "invalid_protocol_takes": [r["key"] for r in complete if not r["audio_protocol_valid"]],
        "takes": rows,
    }
    write_atomic(output / "results.json", encode(summary), replace=True)
    stream = io.StringIO(newline="")
    fields = ["key", "status", "expected_events", "observed_events", "count_matches", "file"]
    fields += [
        "audio_protocol_valid",
        "max_score",
        "event_seconds",
        "estimated_hits",
        "estimated_misses",
        "estimated_extra_events",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, **row.get("assessment", {})})
    write_atomic(output / "summary.csv", stream.getvalue().encode(), replace=True)
    page = [
        "<!doctype html><meta charset='utf-8'><title>Живые тесты ФирАя</title>",
        "<style>body{font:17px sans-serif;max-width:1100px;margin:30px auto}"
        "section{padding:14px;border-bottom:1px solid #bbb}table{border-collapse:collapse}"
        "td,th{padding:8px;border:1px solid #ccc}.warn{color:#a30}</style>",
        "<h1>Живые тесты ФирАя</h1>",
        f"<p>Завершено {len(completed_keys)}/{planned_count}. Оригинальные записи сохранены без "
        f"обработки.</p>",
        "<p class='warn'>Метки слов предполагаются по подсказкам. Совпадение количества "
        "активаций не доказывает правильное распознавание. Прослушайте WAV и сверьте времена.</p>",
        "<p><a href='session.json'>Модель и условия</a> · <a href='results.json'>Все "
        "результаты</a>"
        " · <a href='summary.csv'>CSV</a></p>",
    ]
    for row in rows:
        page.append(
            f"<section><h2>{html.escape(row['key'])}: {html.escape(row['title'])}</h2>"
            f"<audio controls preload='none' src='{html.escape(row['file'], quote=True)}'></audio>"
        )
        if row["status"] == "complete":
            page.append(
                f"<p>Ожидалось {row['expected_events']}; активаций {row['observed_events']}. "
                f"Времена: {html.escape(str(row['event_seconds']))}</p>"
                f"<p>Непрерывность аудио и тайминг подсказок: {row['audio_protocol_valid']}.</p>"
                "<table><tr><th>Подсказка</th><th>Время</th><th>Активации в слоте</th></tr>"
            )
            for slot in row["assessment"]["slots"]:
                page.append(
                    f"<tr><td>{html.escape(slot['text'])}</td><td>{slot['actual_seconds']:.2f}</td>"
                    f"<td>{html.escape(str(slot['event_seconds']))}</td></tr>"
                )
            page.append(
                f"</table><p>Вне слотов: {row['assessment']['outside_slot_events']}</p>"
                f"<p><a href='takes/{row['key']}_scores.json'>Все scores</a></p>"
            )
        else:
            page.append(f"<p class='warn'>Статус: {html.escape(row['status'])}</p>")
        page.append(f"<p>Примечание: {html.escape(row.get('note', ''))}</p></section>")
    write_atomic(output / "review.html", "\n".join(page).encode(), replace=True)
    return summary


def wait_enter(prompt):
    """Начинать следующий дубль только по Enter."""
    while input(prompt).strip():
        print("Нажми только Enter; для остановки Ctrl+C.")


def write_reading_page(output, protocol):
    """Keep long paragraphs visible without scrolling through terminal logs."""
    tests = {task["test"]: task for task in protocol}
    page = [
        "<!doctype html><meta charset='utf-8'><title>Текст длинного KWS-теста</title>",
        "<style>body{font:22px/1.6 sans-serif;max-width:900px;margin:40px auto;padding:15px}"
        "p{margin-bottom:24px}h2{margin-top:60px}.note{font-size:17px;color:#555}</style>",
        "<h1>Текст для чтения</h1><p class='note'>Запись запускается по Enter в терминале. "
        "Начинай после «ГОВОРИ», читай один раз естественно, без выделения имени. "
        "После окончания текста молчи до завершения записи. Эта страница не запускает "
        "микрофон.</p>",
    ]
    for task in tests.values():
        page.append(
            f"<h2>Тест {task['test']}: {html.escape(task['title'])}</h2>"
            f"<p class='note'>Ожидается {task['expected_events']} активаций; два повтора.</p>"
        )
        for cue in task["cues"]:
            page.extend(
                f"<p>{html.escape(paragraph)}</p>" for paragraph in cue["text"].split("\n\n")
            )
    write_atomic(output / "reading.html", "\n".join(page).encode())


def main(argv=None):
    """Провести отдельные записи выбранного набора, сохраняя состояние для --resume."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--experiment", type=Path)
    source.add_argument(
        "--reference-session",
        type=Path,
        help="взять неизменную копию модели и порог из предыдущего сеанса",
    )
    parser.add_argument("--backbone", type=Path, default=DATA / "experiments/_backbone")
    parser.add_argument("--topic", default="/audio/mic")
    parser.add_argument(
        "--seconds",
        type=float,
        help="минимум секунд на дубль (20 по умолчанию); speech автоматически 40–45 с, long 240 с",
    )
    parser.add_argument(
        "--suite",
        choices=tuple(SUITES),
        help="long: два длинных текста; speech: 15 тестов; extended: 11 (по умолчанию); classic: "
        "прежние 7",
    )
    parser.add_argument("--speaker", default="unspecified")
    parser.add_argument("--condition", default="quiet_1m")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--notes", action="store_true", help="необязательное примечание после каждого дубля"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.experiment is None:
        args.experiment = DATA / "experiments/silero_hard_01"
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds < 20):
        parser.error("--seconds должен быть >= 20: последняя подсказка на 17-й секунде")
    if args.resume and args.output is None:
        parser.error("для --resume укажите --output с папкой прежнего сеанса")
    previous_session = None
    if args.resume:
        try:
            previous_session = json.loads((args.output / "session.json").read_text())
        except (OSError, ValueError) as error:
            print(f"Ошибка чтения сеанса: {error}", file=sys.stderr)
            return 2
    suite = args.suite or (
        previous_session.get("suite", "classic") if previous_session else "extended"
    )
    tests = SUITES[suite]
    if args.seconds is None:
        # Recover the original duration even from old sessions without this field.
        args.seconds = (
            previous_session.get(
                "minimum_seconds",
                max(
                    cue["window_end_seconds"]
                    for task in previous_session["protocol"]
                    for cue in task["cues"]
                ),
            )
            if previous_session
            else 20
        )
    protocol = [
        {
            "test": test.number,
            "title": test.title,
            "repeat": repeat,
            "expected_events": test.expected,
            "cues": test.cues(max(args.seconds, test.seconds)),
            **(
                {"requested_seconds": max(args.seconds, test.seconds)}
                if suite in ("speech", "long")
                else {}
            ),
        }
        for test in tests
        for repeat in (1, 2)
    ]
    print(
        f"{len(tests)} тестов × 2 повтора. Говори только после >>> ГОВОРИ; в остальное время "
        f"молчи."
    )
    print("До записи каждой попытки — только Enter. Все слова читать естественно, имя — ФирАя.")
    print("Каждую подсказку произноси ОДИН раз. Не произноси следующий текст до его сигнала.")
    print(
        "Фразу читай слитно, без специальной паузы перед именем; не комментируй результат в "
        "записи."
    )
    print("Не выделяй имя громкостью или темпом. Обычные паузы между предложениями допустимы.")
    print("Если закончил подсказку раньше — молчи; не повторяй её и не добавляй своих слов.")
    print(
        f"Всего чистого аудио: "
        f"{sum(task.get('requested_seconds', args.seconds) for task in protocol) / 60:.1f} мин."
    )
    if args.dry_run:
        print(json.dumps(protocol, ensure_ascii=False, indent=2))
        return 0
    output = args.output or DATA / "evaluation" / (
        datetime.now(timezone.utc).strftime("live_%Y%m%d_%H%M%S_UTC_") + uuid.uuid4().hex[:6]
    )
    lock_owned = False
    try:
        if args.resume:
            session = previous_session
            if (
                session["protocol"] != protocol
                or session["speaker"] != args.speaker
                or session["condition"] != args.condition
                or session["topic"] != args.topic
            ):
                raise ValueError(
                    "Для --resume используйте те же условия, speaker, topic и seconds"
                )
        else:
            output.mkdir(parents=True, exist_ok=False)
            (output / "takes").mkdir()
            (output / "audio").mkdir()
            (output / "model").mkdir()
            reference = (
                json.loads((args.reference_session / "session.json").read_text())
                if args.reference_session
                else None
            )
            config = (
                reference["inference_config"]
                if reference
                else json.loads((args.experiment / "inference_config.json").read_text())
            )
            if config["model"] != "firaya_trial.onnx":
                raise ValueError("ожидается firaya_trial.onnx")
            model_data = (
                (args.reference_session / "model" if reference else args.experiment)
                / config["model"]
            ).read_bytes()
            backbone_hashes = {
                name: hashlib.sha256((args.backbone / name).read_bytes()).hexdigest()
                for name in ("melspectrogram.onnx", "embedding_model.onnx")
            }
            if reference and (
                hashlib.sha256(model_data).hexdigest() != reference["model_sha256"]
                or backbone_hashes != reference["backbone_sha256"]
            ):
                raise ValueError("модель или backbone эталонного сеанса изменились")
            write_atomic(output / "model/firaya_trial.onnx", model_data)
            session = {
                "schema_version": 1,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "experiment": reference["experiment"]
                if reference
                else str(args.experiment.resolve()),
                "inference_config": config,
                "model_sha256": hashlib.sha256(model_data).hexdigest(),
                "backbone_sha256": backbone_hashes,
                "protocol": protocol,
                "speaker": args.speaker,
                "condition": args.condition,
                "suite": suite,
                "minimum_seconds": args.seconds,
                "topic": args.topic,
                "evaluation_only": True,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
            if reference:
                session["reference_session"] = str(args.reference_session.resolve())
            write_atomic(output / "session.json", encode(session))
        if suite == "long" and not (output / "reading.html").exists():
            write_reading_page(output, protocol)
        lock = output / ".recording.lock"
        write_atomic(lock, b"single writer\n")
        lock_owned = True
        config = session["inference_config"]
        model_path = output / "model/firaya_trial.onnx"
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != session["model_sha256"]:
            raise ValueError("копия модели изменилась")
        if any(
            hashlib.sha256((args.backbone / name).read_bytes()).hexdigest() != digest
            for name, digest in session["backbone_sha256"].items()
        ):
            raise ValueError("backbone изменился; не смешиваем разные модели в одном сеансе")
        runtime = load_runtime(model_path, config, args.backbone)
        versions = {}
        for name in ("openwakeword", "onnxruntime", "numpy"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = "unknown"
        versions_path = output / "runtime_versions.json"
        if versions_path.exists():
            if json.loads(versions_path.read_text()) != versions:
                raise ValueError("Версии runtime изменились; создайте новый сеанс")
        else:
            write_atomic(versions_path, encode(versions))
        print(
            f"\nСеанс: {output}\nМодель: {session['experiment']}; порог {config['threshold']:.3f}"
        )
        if suite == "long":
            print(f"Полный текст удобно читать в браузере: {output / 'reading.html'}")
            print(
                "Это непрерывное чтение: внутри текста нет команд МОЛЧИ. Количество активаций — "
                "только предварительная проверка."
            )
        update_reports(output)
        for task in protocol:
            key = f"test_{task['test']:02d}_repeat_{task['repeat']:02d}"
            metadata = output / "takes" / f"{key}.json"
            target = output / "audio" / f"{key}.wav"
            record = json.loads(metadata.read_text()) if metadata.exists() else None
            if record:
                if hashlib.sha256(target.read_bytes()).hexdigest() != record["wav_sha256"]:
                    raise ValueError(f"WAV изменён: {target}")
                if record["status"] == "complete":
                    print(f"Уже завершён, пропускаю {key}")
                    continue
            else:
                if target.exists():
                    raise ValueError(
                        f"WAV без журнала уже существует: {target}; сохранён, не перезаписываем"
                    )
                print(
                    f"\n=== Тест {task['test']}/{len(tests)}: {task['title']}; повтор "
                    f"{task['repeat']}/2 ==="
                )
                duration = task.get("requested_seconds", args.seconds)
                print("Сначала прочитай текст глазами. Не произноси его до >>> ГОВОРИ:")
                for cue in task["cues"]:
                    print(f"  {cue['planned_seconds']:g} с: {cue['text']}")
                print(f"Ожидаем {task['expected_events']} активаций. Длительность {duration:g} с.")
                wait_enter("Приготовься и нажми Enter: ")
                pcm, metrics = capture(args.topic, duration, task["cues"])
                interrupted = metrics["interrupted"]
                if interrupted:
                    key += "_interrupted_" + uuid.uuid4().hex[:6]
                    metadata = output / "takes" / f"{key}.json"
                    target = output / "audio" / f"{key}.wav"
                data = wav_bytes(pcm, metrics["sample_rate"], metrics["channels"])
                write_atomic(target, data)
                record = {
                    "key": key,
                    "test": task["test"],
                    "repeat": task["repeat"],
                    "title": task["title"],
                    "file": str(target.relative_to(output)),
                    "expected_events": task["expected_events"],
                    "requested_seconds": duration,
                    "planned_cues": task["cues"],
                    "capture": metrics,
                    "wav_sha256": hashlib.sha256(data).hexdigest(),
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "status": "interrupted" if interrupted else "captured",
                }
                write_atomic(metadata, encode(record))
                update_reports(output)
                if interrupted:
                    raise KeyboardInterrupt
            result, curve = infer_record(runtime, config, target, record)
            if args.notes:
                result["note"] = input(
                    "Примечание (например, опоздал или назови ошибочное слово; Enter — без него): "
                )
            write_atomic(output / "takes" / f"{key}_scores.json", encode(curve), replace=True)
            write_atomic(metadata, encode(result), replace=True)
            update_reports(output)
            print(
                f"РЕЗУЛЬТАТ: {result['observed_events']} активаций; ожидалось "
                f"{result['expected_events']}."
            )
            print(
                "Времена: " + (", ".join(f"{s:.2f} с" for s in result["event_seconds"]) or "нет")
            )
            for slot in result["assessment"]["slots"]:
                print(
                    f"  {slot['actual_seconds']:.2f} с — {slot['text']}: "
                    f"{len(slot['event_seconds'])} активаций в слоте"
                )
            if not result["audio_protocol_valid"]:
                print(
                    "ВНИМАНИЕ: разрывы/недостаточная длительность/задержка подсказок. Проверь "
                    "отчёт."
                )
            print("Сопоставление по подсказкам предварительное; WAV и scores сохранены.")
        final = update_reports(output)
        print("\nСводка (активации / запланированные произнесения имени):")
        for test in tests:
            takes = [
                r for r in final["takes"] if r["test"] == test.number and r["status"] == "complete"
            ]
            print(
                f"Тест {test.number}: "
                + " | ".join(f"{r['observed_events']}/{r['expected_events']}" for r in takes)
            )
        print(
            f"\nГотово: {final['completed_takes']}/{len(protocol)}. Отчёт: "
            f"{output / 'review.html'}"
        )
        return 0
    except (KeyboardInterrupt, EOFError):
        print(f"\nОстановлено. Сохранённые записи не удалены. Сеанс: {output}")
        print(f"Продолжение: добавь --resume --output {output} с теми же параметрами")
        return 130
    except (OSError, ValueError, RuntimeError, ImportError, KeyError) as error:
        print(f"Ошибка: {error}\nСохранённые файлы: {output}", file=sys.stderr)
        return 2
    finally:
        if lock_owned:
            (output / ".recording.lock").unlink()


if __name__ == "__main__":
    raise SystemExit(main())
