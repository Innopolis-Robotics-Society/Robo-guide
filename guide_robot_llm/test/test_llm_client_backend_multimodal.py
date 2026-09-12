"""`llm_client.backend` -- multimodal, capability, stage-timing (Taiga #3)."""

from __future__ import annotations

import threading
import time

import pytest

from guide_robot_llm.llm_client.backend import Backend, BackendConfig, build_content
from guide_robot_llm.llm_client.errors import (
    BackendAborted,
    BackendError,
    BackendHTTPError,
    BackendTimeout,
)
from guide_robot_llm.llm_client.telemetry import (
    STAGE_GENERATION,
    STAGE_NETWORK,
    ClientTelemetry,
)
from test.mocks.mock_llm_server import MockLlmServer

_PAYLOAD = "QUJCDQ==" * 50  # 400 байт base64
_DATA_URL = f"data:image/jpeg;base64,{_PAYLOAD}"
_TEXT_MESSAGES = [{"role": "user", "content": "привет"}]
_MM_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "что на картинке?"},
            {"type": "image_url", "image_url": {"url": _DATA_URL}},
        ],
    }
]


@pytest.fixture
def mock_server():
    server = MockLlmServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _backend(
    server: MockLlmServer,
    *,
    multimodal_enabled: bool = False,
    max_images: int = 0,
    model_name: str = "",
    extra_headers: dict[str, str] | None = None,
) -> Backend:
    return Backend(
        BackendConfig(
            base_url=server.url,
            read_timeout_s=5.0,
            model_name=model_name,
            extra_headers=extra_headers or {},
            multimodal_enabled=multimodal_enabled,
            max_images=max_images,
        )
    )


# -- text-only поведение не изменилось (AC: existing tests remain compatible)


def test_text_only_request_body_unchanged(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["ок"]
    backend = _backend(mock_server)

    backend.complete(_TEXT_MESSAGES)

    body = mock_server.last_request_body
    assert body["messages"][0]["content"] == "привет"  # строка, не массив
    assert "model" not in body
    assert body["stream"] is True


def test_build_content_no_frames_is_plain_string() -> None:
    assert build_content("привет") == "привет"


def test_build_content_with_frames_is_content_array() -> None:
    content = build_content("что тут?", [_DATA_URL, _DATA_URL])

    assert content[0] == {"type": "text", "text": "что тут?"}
    assert content[1] == {"type": "image_url", "image_url": {"url": _DATA_URL}}
    assert content[2]["type"] == "image_url"


# -- кадры доходят до эндпоинта (AC: new tests prove image data reaches)


def test_image_data_reaches_endpoint(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["картина"]
    backend = _backend(mock_server, multimodal_enabled=True)

    result = backend.complete(_MM_MESSAGES)

    assert result.text == "картина"
    content = mock_server.last_request_body["messages"][0]["content"]
    assert content[1]["image_url"]["url"] == _DATA_URL  # data-URL целиком
    validation = mock_server.last_request_meta["content_validation"]
    assert validation["ok"] is True
    assert validation["kinds"] == ["text", "image_url"]


# -- capability-конфигурация (Taiga #3: model, headers, multimodal, max_images)


def test_model_name_goes_into_payload_when_set(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["ок"]
    backend = _backend(mock_server, model_name="qwen-vl-7b")

    backend.complete(_TEXT_MESSAGES)

    assert mock_server.last_request_body["model"] == "qwen-vl-7b"


def test_extra_headers_reach_server(mock_server: MockLlmServer) -> None:
    mock_server.chunks = ["ок"]
    backend = _backend(mock_server, extra_headers={"X-Client": "guide_robot_llm"})

    backend.complete(_TEXT_MESSAGES)

    assert mock_server.last_request_meta["headers"]["X-Client"] == "guide_robot_llm"


def test_image_to_text_only_endpoint_refused_before_http(mock_server: MockLlmServer) -> None:
    backend = _backend(mock_server)  # multimodal_enabled=false по дефолту

    with pytest.raises(BackendError):
        backend.complete(_MM_MESSAGES)

    assert mock_server.request_count == 0  # HTTP-запрос не уходил


def test_max_images_exceeded_refused_before_http(mock_server: MockLlmServer) -> None:
    backend = _backend(mock_server, multimodal_enabled=True, max_images=1)
    two_frames = build_content("два кадра", [_DATA_URL, _DATA_URL])

    with pytest.raises(BackendError):
        backend.complete([{"role": "user", "content": two_frames}])

    assert mock_server.request_count == 0


# -- redaction: секреты не светятся в error strings (AC #3)


def test_secret_header_never_in_error_string(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_HTTP_ERROR
    mock_server.http_status = 500
    backend = _backend(mock_server, extra_headers={"X-Api-Key": "sekret-42"})

    with pytest.raises(BackendHTTPError) as excinfo:
        backend.complete(_TEXT_MESSAGES)

    assert excinfo.value.status_code == 500
    assert "sekret-42" not in str(excinfo.value)


def test_image_payload_never_in_error_string(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_MID_STREAM_FAILURE
    mock_server.chunks = ["раз", "два", "три"]
    backend = _backend(mock_server, multimodal_enabled=True)

    with pytest.raises(BackendError) as excinfo:
        backend.complete(_MM_MESSAGES)

    assert _PAYLOAD not in str(excinfo.value)


# -- stage-timing и атрибуция отказов (Taiga #3 research evidence)


def test_stage_timings_with_delayed_first_token(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_DELAYED_FIRST_TOKEN
    mock_server.first_token_delay_s = 0.4
    mock_server.chunks = ["раз", "два"]
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    snap = telemetry.snapshot()
    assert len(snap["timings"]) == 1
    t = snap["timings"][0]
    assert t["serialization_ms"] >= 0
    assert t["upload_ms"] >= 0
    assert t["ttft_ms"] >= 350, f"TTFT {t['ttft_ms']}ms -- меньше задержки 1-го токена"
    assert t["full_ms"] >= t["ttft_ms"]
    # Без stop_when parse-ready совпадает с full.
    assert t["parse_ready_ms"] == t["full_ms"]


def test_parse_ready_earlier_than_ttft_impossible_and_set_on_stop_when(
    mock_server: MockLlmServer,
) -> None:
    # stop_when срабатывает на 2-м чанке -- parse-ready позже TTFT (1-й чанк).
    mock_server.mode = MockLlmServer.MODE_SLOW
    mock_server.chunk_delay_s = 0.15
    mock_server.chunks = ['{"tool":"', "reply", '","args":{}}', "SHOULD_NOT"]
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    result = backend.complete(
        _TEXT_MESSAGES, stop_when=lambda text: '"tool":"reply"' in text, telemetry=telemetry
    )

    t = telemetry.snapshot()["timings"][0]
    assert "SHOULD_NOT" not in result.text
    assert result.finish_reason == "stop_when"
    assert t["parse_ready_ms"] is not None
    assert t["parse_ready_ms"] > t["ttft_ms"]


def test_failure_stage_network_on_read_timeout_before_first_byte(
    mock_server: MockLlmServer,
) -> None:
    mock_server.mode = MockLlmServer.MODE_HANG
    mock_server.hang_s = 5.0
    backend = Backend(BackendConfig(base_url=mock_server.url, read_timeout_s=0.3))
    telemetry = ClientTelemetry()

    with pytest.raises(BackendTimeout):
        backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    assert telemetry.snapshot()["last_failure"] == {"stage": STAGE_NETWORK, "kind": "timeout"}


def test_failure_stage_network_on_disconnect(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_DISCONNECT
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    with pytest.raises(BackendError):
        backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    assert telemetry.snapshot()["last_failure"] == {"stage": STAGE_NETWORK, "kind": "error"}


def test_failure_stage_generation_on_malformed_json(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_MALFORMED_JSON
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    with pytest.raises(BackendError):
        backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    assert telemetry.snapshot()["last_failure"] == {"stage": STAGE_GENERATION, "kind": "error"}


def test_failure_stage_generation_on_mid_stream_failure(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_MID_STREAM_FAILURE
    mock_server.chunks = ["раз", "два", "три"]
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    with pytest.raises(BackendError):
        backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    assert telemetry.snapshot()["last_failure"] == {"stage": STAGE_GENERATION, "kind": "error"}


def test_http_error_counted_as_network_http_failure(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_HTTP_ERROR
    mock_server.http_status = 503
    backend = _backend(mock_server)
    telemetry = ClientTelemetry()

    with pytest.raises(BackendHTTPError):
        backend.complete(_TEXT_MESSAGES, telemetry=telemetry)

    snap = telemetry.snapshot()
    assert snap["http_failures"] == 1
    assert snap["failure_stages"][STAGE_NETWORK] == 1


# -- abort закрывает активный multimodal-стрим (AC #3)


def test_abort_closes_active_multimodal_stream(mock_server: MockLlmServer) -> None:
    mock_server.mode = MockLlmServer.MODE_SLOW
    mock_server.chunks = ["раз", "два", "три", "четыре"]
    mock_server.chunk_delay_s = 0.3
    backend = _backend(mock_server, multimodal_enabled=True)
    abort_event = threading.Event()
    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            backend.complete(_MM_MESSAGES, abort_event=abort_event)
        except BackendAborted as error:
            outcome["error"] = error

    thread = threading.Thread(target=_run)
    start = time.monotonic()
    thread.start()
    time.sleep(0.1)  # дать прийти первому чанку (кадр уже ушёл на сервер)
    abort_event.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not thread.is_alive(), "поток не завершился -- abort не сработал"
    assert isinstance(outcome.get("error"), BackendAborted)
    assert elapsed < 1.0, f"abort занял {elapsed:.2f}с -- дольше одного chunk_delay_s"
