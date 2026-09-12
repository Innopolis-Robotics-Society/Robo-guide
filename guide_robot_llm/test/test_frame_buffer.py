"""Юнит-тесты `lib/frame_buffer.py` (Taiga #2) -- чистый Python, без ROS.

Покрытие: выборка кадров из окна, age-rejection, мусорные/oversize кадры
(отбрасываются и считаются), даунскейл >1280 px с перекодированием q80,
бюджет совокупного payload при freeze, ограниченная память кольца,
конкурентные offer/freeze (callback'и ROS идут в reentrant-группе).
"""

from __future__ import annotations

import base64
import io
import threading

from PIL import Image

from guide_robot_llm.lib.frame_buffer import FrameBuffer, FrozenFrame

_PREFIX = "data:image/jpeg;base64,"


def make_jpeg(width: int, height: int, color: tuple[int, int, int] = (120, 60, 30)) -> bytes:
    """JPEG-байты размера width x height -- синтетика, без камеры."""
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def jpeg_size(data_url: str) -> tuple[int, int]:
    """(w, h) JPEG-а внутри data-URL -- для проверки даунскейла."""
    payload = base64.b64decode(data_url[len(_PREFIX) :])
    with Image.open(io.BytesIO(payload)) as image:
        return image.size


def jpeg_bytes(data_url: str) -> bytes:
    return base64.b64decode(data_url[len(_PREFIX) :])


class _Clock:
    """Голодные часы теста: time -- ручная, offer/freeze берут .now()."""

    def __init__(self, start: float = 1000.0) -> None:
        self.time = start

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> None:
        self.time += seconds


def test_empty_freeze_returns_empty() -> None:
    buffer = FrameBuffer()
    assert buffer.freeze(1000.0) == []


def test_single_frame_freeze() -> None:
    buffer = FrameBuffer()
    jpeg = make_jpeg(320, 240)
    buffer.offer(jpeg, captured_at=999.5)

    frozen = buffer.freeze(1000.0)

    assert len(frozen) == 1
    frame = frozen[0]
    assert isinstance(frame, FrozenFrame)
    assert frame.captured_at == 999.5
    assert frame.payload_bytes == len(jpeg)
    assert frame.data_url.startswith(_PREFIX)
    assert jpeg_bytes(frame.data_url) == jpeg


def test_sampling_evenly_spaced_up_to_frame_count() -> None:
    buffer = FrameBuffer(frame_count=3)
    for i in range(5):
        buffer.offer(make_jpeg(32, 32, (i * 40, 0, 0)), captured_at=999.6 + i * 0.1)

    # 5 кадров в окне, frame_count=3 -> индексы 0, 2, 4 (ровный шаг).
    frozen = buffer.freeze(1000.0)
    assert [round(f.captured_at, 6) for f in frozen] == [999.6, 999.8, 1000.0]


def test_sampling_returns_all_when_fewer_than_frame_count() -> None:
    buffer = FrameBuffer(frame_count=3)
    buffer.offer(make_jpeg(32, 32), captured_at=999.7)
    buffer.offer(make_jpeg(32, 32), captured_at=999.9)

    assert len(buffer.freeze(1000.0)) == 2


def test_frame_count_one_picks_freshest() -> None:
    buffer = FrameBuffer(frame_count=1)
    buffer.offer(make_jpeg(32, 32), captured_at=999.0)
    buffer.offer(make_jpeg(32, 32), captured_at=999.9)

    frozen = buffer.freeze(1000.0)
    assert len(frozen) == 1
    assert frozen[0].captured_at == 999.9


def test_stale_frames_rejected() -> None:
    buffer = FrameBuffer(max_frame_age_s=2.0)
    buffer.offer(make_jpeg(32, 32), captured_at=997.9)  # 2.1 c -- старше окна
    buffer.offer(make_jpeg(32, 32), captured_at=998.5)  # 1.5 c -- свежий

    frozen = buffer.freeze(1000.0)
    assert [f.captured_at for f in frozen] == [998.5]


def test_stale_frames_evicted_from_ring() -> None:
    """Кольцо не копит кадры старше окна: память ограничена окном + capacity."""
    buffer = FrameBuffer(max_frame_age_s=2.0, max_buffered_frames=64)
    buffer.offer(make_jpeg(32, 32), captured_at=990.0)
    buffer.offer(make_jpeg(32, 32), captured_at=999.9)

    assert buffer.size() == 1
    assert buffer.freeze(1000.0)


def test_corrupt_frames_discarded_and_counted() -> None:
    buffer = FrameBuffer()
    buffer.offer(b"this is not jpeg", captured_at=999.5)
    # Валидный заголовок, мусор дальше -- PIL decode упадёт.
    buffer.offer(b"\xff\xd8\xff\xe0" + b"\x00" * 64, captured_at=999.6)

    assert buffer.size() == 0
    assert buffer.freeze(1000.0) == []
    assert buffer.stats["discarded_corrupt"] == 2


def test_oversized_frames_discarded_and_counted() -> None:
    buffer = FrameBuffer(max_payload_bytes=500)
    buffer.offer(make_jpeg(320, 240), captured_at=999.5)  # ~2-3 KB > 500 B

    assert buffer.size() == 0
    assert buffer.stats["discarded_oversized"] == 1


def test_downscale_above_max_long_edge() -> None:
    """2000 px > 1280 -> перекодирование: длинная сторона <= 1280, пропорции те же."""
    buffer = FrameBuffer(max_long_edge_px=1280)
    original = make_jpeg(2000, 1000)
    buffer.offer(original, captured_at=999.5)

    frozen = buffer.freeze(1000.0)
    assert len(frozen) == 1
    width, height = jpeg_size(frozen[0].data_url)
    assert max(width, height) <= 1280
    assert abs(width / height - 2.0) < 0.05  # пропорции сохранены
    assert jpeg_bytes(frozen[0].data_url) != original  # реально перекодирован


def test_no_reencode_within_limit() -> None:
    """800 px <= 1280 -- байты не трогаем (камерное качество не портим зря)."""
    buffer = FrameBuffer(max_long_edge_px=1280)
    original = make_jpeg(800, 600)
    buffer.offer(original, captured_at=999.5)

    frozen = buffer.freeze(1000.0)
    assert jpeg_bytes(frozen[0].data_url) == original


def test_payload_budget_drops_oldest_at_freeze() -> None:
    """Совокупный payload > бюджета -- выкидываем с самого старого."""
    buffer = FrameBuffer(frame_count=3, max_payload_bytes=9000)
    # Три кадра по ~4 KB: все по отдельности в норме, вместе -- ~12 KB > 9 KB.
    for i in range(3):
        buffer.offer(make_jpeg(640, 480, (i * 60, 0, 0)), captured_at=999.5 + i * 0.2)

    frozen = buffer.freeze(1000.0)
    assert 1 <= len(frozen) <= 3
    assert sum(f.payload_bytes for f in frozen) <= 9000
    # Остался самый свежий (старые выкинуты).
    assert frozen[-1].captured_at == 999.9


def test_ring_is_bounded_by_capacity() -> None:
    buffer = FrameBuffer(lookback_s=100.0, max_frame_age_s=100.0, max_buffered_frames=8)
    for i in range(20):
        buffer.offer(make_jpeg(32, 32), captured_at=1000.0 + i * 0.01)

    assert buffer.size() == 8
    # Выжившие -- самые свежие (20 кадров, capacity 8 -> последние 8).
    frozen = buffer.freeze(1000.2)
    assert frozen[-1].captured_at == 1000.19


def test_concurrent_offers_and_freezes() -> None:
    """8 потоков одновременно offer'ят и freeze'ят -- без ошибок и расползания."""
    buffer = FrameBuffer(max_buffered_frames=64)
    errors: list[BaseException] = []
    jpeg = make_jpeg(160, 120)

    def worker(offset: int) -> None:
        try:
            clock = _Clock(1000.0 + offset * 0.0001)
            for _ in range(200):
                buffer.offer(jpeg, captured_at=clock.now())
                for frame in buffer.freeze(clock.now()):
                    assert frame.data_url.startswith(_PREFIX)
                clock.advance(0.001)
        except BaseException as exc:  # noqa: BLE001 -- собираем в общий список
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert buffer.size() <= 64
    # Кольцо не потеряло кадры из последних миллисекунд.
    assert buffer.freeze(1000.21)


def test_freeze_is_non_mutating() -> None:
    """Freeze не дестабилизирует кольцо: повторный freeze даёт те же кадры."""
    buffer = FrameBuffer(frame_count=3)
    for i in range(3):
        buffer.offer(make_jpeg(32, 32), captured_at=999.6 + i * 0.1)

    first = buffer.freeze(1000.0)
    second = buffer.freeze(1000.0)
    assert [f.captured_at for f in first] == [f.captured_at for f in second]
    assert buffer.size() == 3


def test_stats_are_cumulative() -> None:
    buffer = FrameBuffer(max_payload_bytes=500)
    buffer.offer(make_jpeg(320, 240), captured_at=999.5)
    buffer.offer(b"garbage", captured_at=999.6)
    stats = buffer.stats
    stats["discarded_oversized"] += 0  # просто фиксируем форму dict
    assert stats["discarded_oversized"] == 1
    assert stats["discarded_corrupt"] == 1
