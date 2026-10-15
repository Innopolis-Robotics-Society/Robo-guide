"""Юниты на фабрику TTS-бэкендов без модели."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import numpy as np
import pytest

from guide_robot_voice.lib.backends import (
    FallbackBackend,
    NullBackend,
    SileroBackend,
    TtsServiceError,
    YandexBackend,
    build_silero_ssml,
    make_backend,
    parse_yandex_voice,
    strip_yandex_markup,
    trim_trailing_silence,
)


def test_make_backend_silero_without_load() -> None:
    backend = make_backend("silero", model_path="/no/such.pt", speaker="xenia")
    assert backend.sample_rate == 48000
    with pytest.raises(RuntimeError, match="load"):
        next(backend.synthesize("привет"))


def test_silero_load_missing_file() -> None:
    backend = SileroBackend(model_path="/no/such.pt")
    with pytest.raises(FileNotFoundError, match="v5_ru"):
        backend.load()


def test_silero_rejects_device_rate_as_synthesis_rate() -> None:
    """XVF3800 16кГц -- частота устройства, не допустимый output Silero v5."""
    with pytest.raises(ValueError, match="не поддерживает 16000"):
        SileroBackend(model_path="/no/such.pt", sample_rate=16000)
def test_ssml_not_built_for_defaults() -> None:
    """При темпе 100% и модельной паузе apply_tts зовётся с text, не ssml."""
    assert build_silero_ssml("Привет. Как дела?") is None
    assert build_silero_ssml("Привет.", rate=" 100% ", sentence_pause_ms=0) is None


def test_ssml_rate_and_breaks_with_escaping() -> None:
    """Предложения разделены <break>, темп обёрнут в <prosody>, спецсимволы экранированы."""
    ssml = build_silero_ssml("Привет! Тут <5 & больше. Ясно?", rate="120%", sentence_pause_ms=150)
    assert ssml == (
        '<speak><prosody rate="120%">Привет!<break time="150ms"/>'
        'Тут &lt;5 &amp; больше.<break time="150ms"/>Ясно?</prosody></speak>'
    )


def test_ssml_rate_only_keeps_text_whole() -> None:
    """Без sentence_pause_ms предложения не режутся, модельная пауза остаётся."""
    assert (
        build_silero_ssml("А. Б.", rate="fast")
        == '<speak><prosody rate="fast">А. Б.</prosody></speak>'
    )


def test_trim_trailing_silence() -> None:
    """Хвост тише 200 LSB укорачивается до keep_ms; речь и -1 не трогаются."""
    sr = 1000
    pcm = np.concatenate([np.full(500, 3000, np.int16), np.full(500, 5, np.int16)])
    assert trim_trailing_silence(pcm, 100, sr).size == 600
    assert trim_trailing_silence(pcm, 0, sr).size == 500
    assert trim_trailing_silence(pcm, -1, sr).size == 1000
    assert trim_trailing_silence(pcm, 900, sr).size == 1000  # не длиннее исходного
    quiet = np.full(300, 5, np.int16)
    assert trim_trailing_silence(quiet, 10, sr).size == 300  # сплошная тишина -- как есть


# -- Yandex SpeechKit ------------------------------------------------------


class _FakeResponse:
    """requests.Response для потокового ответа: чанки байтов, учёт close()."""

    def __init__(self, chunks: list[bytes], status_code: int = 200, text: str = "") -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size: object = None) -> Iterator[bytes]:
        del chunk_size
        yield from self._chunks

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


def _audio_message(pcm: np.ndarray) -> bytes:
    data = base64.b64encode(pcm.astype("<i2").tobytes()).decode()
    return (json.dumps({"result": {"audioChunk": {"data": data}}}) + "\n").encode()


def _yandex(response: _FakeResponse, **kwargs: object) -> tuple[YandexBackend, _FakeSession]:
    backend = YandexBackend(sample_rate=1000, block_ms=100, **kwargs)  # блок 100 сэмплов
    session = _FakeSession(response)
    backend._session = session  # noqa: SLF001 -- без сети и ключа
    return backend, session


def test_strip_yandex_markup() -> None:
    """Символы разметки SpeechKit из текста ЛЛМ не уходят в синтез как разметка."""
    assert strip_yandex_markup("Язык C++ и **жирный** <[small]> [[a]]") == (
        "Язык C плюс плюс и жирный small a"
    )


@pytest.mark.parametrize(
    ("voice", "expected"),
    [
        ("", ("dasha", "friendly")),
        ("7", ("dasha", "friendly")),
        ("masha", ("masha", "")),
        ("masha:strict", ("masha", "strict")),
        (":good", ("dasha", "good")),
    ],
)
def test_parse_yandex_voice(voice: str, expected: tuple[str, str]) -> None:
    assert parse_yandex_voice(voice, "dasha", "friendly") == expected


def test_yandex_request_body() -> None:
    backend = YandexBackend(voice="irina", role="narrator", model="livetts", sample_rate=16000)
    body = backend.build_request("Привет + мир", voice=":casual")
    assert body == {
        "text": "Привет плюс мир",
        "hints": [{"voice": "irina"}, {"role": "casual"}],
        "outputAudioSpec": {
            "rawAudio": {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": "16000"}
        },
        "model": "livetts",
    }


def test_yandex_streams_blocks_across_split_chunks() -> None:
    """Сообщения, разрезанные сетью посередине, собираются; PCM режется на блоки."""
    first = np.arange(150, dtype=np.int16)
    second = np.arange(150, 200, dtype=np.int16)
    stream = _audio_message(first) + _audio_message(second)
    chunks = [stream[:7], stream[7:61], stream[61:]]
    backend, session = _yandex(_FakeResponse(chunks))

    blocks = list(backend.synthesize("текст"))

    assert [block.size for block in blocks] == [100, 50, 50]
    assert np.array_equal(np.concatenate(blocks), np.arange(200, dtype=np.int16))
    assert session.requests[0]["stream"] is True
    assert session.response.closed


def test_yandex_abandoned_generator_closes_response() -> None:
    """Отмена (вызывающий бросил генератор) закрывает HTTP-ответ."""
    response = _FakeResponse([_audio_message(np.zeros(300, dtype=np.int16))])
    backend, _ = _yandex(response)
    stream = backend.synthesize("текст")
    next(stream)
    stream.close()
    assert response.closed


def test_yandex_http_error_raises_with_body() -> None:
    backend, _ = _yandex(_FakeResponse([], status_code=401, text='{"message":"Unauthorized"}'))
    with pytest.raises(RuntimeError, match="HTTP 401.*Unauthorized"):
        list(backend.synthesize("текст"))


def test_yandex_stream_error_and_truncation_raise() -> None:
    backend, _ = _yandex(_FakeResponse([b'{"error": {"grpcCode": 8}}\n']))
    with pytest.raises(RuntimeError, match="grpcCode"):
        list(backend.synthesize("текст"))

    backend, _ = _yandex(_FakeResponse([b'{"result": {"audioChunk": {"da']))
    with pytest.raises(RuntimeError, match="оборванный"):
        list(backend.synthesize("текст"))


def test_yandex_load_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YANDEX_TTS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YANDEX_TTS_API_KEY"):
        YandexBackend().load()
    monkeypatch.setenv("YANDEX_TTS_API_KEY", "key")
    with pytest.raises(ValueError, match="livetts"):
        YandexBackend(model="livetts", speed="1.2").load()


def test_make_backend_yandex() -> None:
    assert make_backend("yandex", sample_rate=16000).sample_rate == 16000


def test_yandex_error_persistence() -> None:
    """Деньги/ключ (HTTP 4xx, gRPC 7/16) -- надолго; 429, 5xx, обрыв -- временно."""
    cases = [
        (_FakeResponse([], status_code=403, text="billing"), True),
        (_FakeResponse([], status_code=429), False),
        (_FakeResponse([], status_code=503), False),
        (_FakeResponse([b'{"error": {"grpcCode": 7}}']), True),
        (_FakeResponse([b'{"error": {"grpcCode": 14}}']), False),
    ]
    for response, persistent in cases:
        backend, _ = _yandex(response)
        with pytest.raises(TtsServiceError) as error:
            list(backend.synthesize("текст"))
        assert error.value.persistent is persistent


# -- страховка облака ---------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _ScriptedBackend:
    """Основной бэкенд: падает, пока задано исключение, иначе отдаёт 2 блока."""

    def __init__(self, sample_rate: int = 1000) -> None:
        self.sample_rate = sample_rate
        self.error: Exception | None = None
        self.fail_after_first_block = False
        self.calls = 0
        self.load_error: Exception | None = None

    def load(self) -> None:
        if self.load_error is not None:
            raise self.load_error

    def synthesize(self, text: str, voice: str = "") -> Iterator[np.ndarray]:
        del text, voice
        self.calls += 1
        if self.error is not None and not self.fail_after_first_block:
            raise self.error
        yield np.full(10, 7, np.int16)
        if self.error is not None:
            raise self.error
        yield np.full(10, 7, np.int16)

    def close(self) -> None:
        pass


def _fallback(primary: _ScriptedBackend) -> tuple[FallbackBackend, _Clock, NullBackend]:
    clock = _Clock()
    spare = NullBackend(sample_rate=2000, chars_per_second=1000.0)  # тон, «текст» = 10 сэмплов
    backend = FallbackBackend(
        primary, spare, retry_s=30.0, persistent_retry_s=300.0, block_ms=10, clock=clock
    )
    backend.load()
    return backend, clock, spare


def _is_primary(blocks: list[np.ndarray]) -> bool:
    return bool(blocks) and all(np.all(block == 7) for block in blocks)


def test_fallback_uses_primary_when_healthy() -> None:
    primary = _ScriptedBackend()
    backend, _, _ = _fallback(primary)
    assert _is_primary(list(backend.synthesize("текст", "dasha:good")))
    assert backend.degraded_reason == ""


def test_fallback_on_no_money_and_returns_after_pause() -> None:
    """Нет денег -> запасной сразу и 300 с без попыток; пополнили -> основной снова."""
    primary = _ScriptedBackend()
    primary.error = TtsServiceError("HTTP 403: billing", persistent=True)
    backend, clock, _ = _fallback(primary)

    spoken = list(backend.synthesize("текст"))
    assert spoken and not _is_primary(spoken)
    assert backend.sample_rate == 1000  # запасной 2000 Гц пересчитан к частоте основного
    assert sum(block.size for block in spoken) == 5
    assert "403" in backend.degraded_reason

    clock.now = 299.0
    list(backend.synthesize("текст"))
    assert primary.calls == 1  # на паузе облако не дёргаем

    primary.error = None  # баланс пополнили
    clock.now = 301.0
    assert _is_primary(list(backend.synthesize("текст")))
    assert backend.degraded_reason == ""


def test_fallback_transient_error_short_pause() -> None:
    primary = _ScriptedBackend()
    primary.error = ConnectionError("нет сети")
    backend, clock, _ = _fallback(primary)
    assert not _is_primary(list(backend.synthesize("текст")))
    primary.error = None
    clock.now = 31.0
    assert _is_primary(list(backend.synthesize("текст")))


def test_fallback_mid_clause_failure_reraises_then_spare() -> None:
    """Обрыв после первого кадра -- исключение (tts_node повторит клаузу), повтор -- запасной."""
    primary = _ScriptedBackend()
    primary.error = TtsServiceError("обрыв", persistent=False)
    primary.fail_after_first_block = True
    backend, _, _ = _fallback(primary)
    with pytest.raises(TtsServiceError):
        list(backend.synthesize("текст"))
    assert not _is_primary(list(backend.synthesize("текст")))


def test_fallback_primary_load_failure_keeps_spare() -> None:
    """Нет ключа на старте -- нода всё равно говорит, запасным."""
    primary = _ScriptedBackend()
    primary.load_error = RuntimeError("YANDEX_TTS_API_KEY пуста")
    backend, clock, _ = _fallback(primary)
    clock.now = 1e6
    assert not _is_primary(list(backend.synthesize("текст")))
    assert primary.calls == 0
    assert "YANDEX_TTS_API_KEY" in backend.degraded_reason
