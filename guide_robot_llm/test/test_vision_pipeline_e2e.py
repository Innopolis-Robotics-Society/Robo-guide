"""Камера end-to-end (Taiga #2): синтетический кадр -> ход диалога -> запись.

`ToolBrokerTestHarness` + `MockLlmServer` (как в test_dialog_agent_e2e.py),
только вместо микрофона -- синтетические `CompressedImage` на
`/camera/image_raw/compressed`. Живая камера здесь не нужна: весь путь
подписки -> кольцевой буфер -> freeze на транскрипте -> `snap["frames"]` ->
запись interaction-лога проверяется на заведомо известных байтах JPEG.

Три AC issue:
1. vision.enabled=false (по умолчанию) -- ключа `frames` в снимке нет;
2. vision.enabled=true без камеры -- ключ есть, список пуст (text-only ход);
3. свежий кадр -> `snapshot.frames` в записи хода несёт наш data-URL.
"""

from __future__ import annotations

import base64
import io
import json
import time

from PIL import Image
from rclpy.parameter import Parameter
from sensor_msgs.msg import CompressedImage

from guide_robot_llm.lib.qos import QOS_VISION_COMPRESSED
from guide_robot_msgs.msg import Transcript
from test.mocks.harness import ToolBrokerTestHarness, wait_until

_NOOP = json.dumps({"tool": "reply", "args": {}})
_PREFIX = "data:image/jpeg;base64,"


def _jpeg(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Синтетический JPEG без камеры."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _dialog_agent_has_mission_state(harness: ToolBrokerTestHarness):
    return lambda: harness.dialog_agent.last_mission_state() is not None


def _publish_frame_until_received(harness: ToolBrokerTestHarness, client, jpeg: bytes) -> None:
    """Стримить кадр, пока буфер его не примет (реальная камера стримит непрерывно;
    одиночный кадр ДО установления DDS-соединения с VOLATILE QoS теряется)."""
    buffer = harness.dialog_agent._frame_buffer  # noqa: SLF001 -- тестовая интроспекция
    pub = client.create_publisher(
        CompressedImage,
        "/camera/image_raw/compressed",
        QOS_VISION_COMPRESSED,
    )
    deadline = time.monotonic() + 5.0
    while buffer.size() < 1 and time.monotonic() < deadline:
        msg = CompressedImage()
        msg.format = "jpeg"
        msg.data = jpeg
        pub.publish(msg)
        time.sleep(0.05)


def _publish_transcript(client, text: str) -> None:
    pub = client.create_publisher(Transcript, "/asr/transcript", 10)
    pub.publish(Transcript(utterance_id=1, text=text, is_final=True))


def _log_lines(harness: ToolBrokerTestHarness) -> list[dict]:
    path = harness.interaction_log._sink.path  # noqa: SLF001 -- тестовая интроспекция
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_text_turn(harness: ToolBrokerTestHarness, client, text: str) -> dict:
    """Один полный ход (noop-действие -> реплика -> say -> запись в лог)."""
    harness.llm_server.chunks_no_grammar = ["Привет!"]
    harness.llm_server.chunks_with_grammar = [_NOOP]
    _publish_transcript(client, text)
    wait_until(lambda: len(_log_lines(harness)) >= 1, timeout_s=15.0)
    return _log_lines(harness)[-1]


def test_vision_disabled_by_default_snapshot_has_no_frames() -> None:
    """AC #2.1: дефолтный запуск (без камеры) -- `frames` в снимке не появляется."""
    harness = ToolBrokerTestHarness()
    try:
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        client = harness.make_client_node()
        record = _run_text_turn(harness, client, "робот, привет")
        assert "frames" not in record["snapshot"]
    finally:
        harness.shutdown()


def test_vision_enabled_without_camera_turn_is_text_only() -> None:
    """AC #2.2: vision.enabled, камеры нет -- ключ есть, список пуст, ход жив."""
    harness = ToolBrokerTestHarness(
        dialog_agent_overrides=(Parameter("vision.enabled", value=True),)
    )
    try:
        assert (
            harness.dialog_agent._frame_buffer is not None  # noqa: SLF001 -- интроспекция
        ), "при vision.enabled буфер должен быть создан в configure"
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        client = harness.make_client_node()
        record = _run_text_turn(harness, client, "робот, привет")
        assert record["snapshot"]["frames"] == []
        assert record["answer_text"], "text-only ход должен был ответить"
    finally:
        harness.shutdown()


def test_vision_enabled_frozen_frame_lands_in_snapshot() -> None:
    """AC #2.3: свежий синтетический кадр -> data-URL в `snapshot.frames` записи хода."""
    harness = ToolBrokerTestHarness(
        dialog_agent_overrides=(
            Parameter("vision.enabled", value=True),
            Parameter("vision.frame_count", value=1),
        )
    )
    try:
        jpeg = _jpeg(320, 240, (200, 100, 50))
        wait_until(_dialog_agent_has_mission_state(harness), timeout_s=5.0)
        client = harness.make_client_node()
        _publish_frame_until_received(harness, client, jpeg)

        record = _run_text_turn(harness, client, "робот, что это")

        frames = record["snapshot"]["frames"]
        assert len(frames) == 1
        assert frames[0].startswith(_PREFIX)
        # 320 px < 1280 -- даунскейл не срабатывает, байты прошли без перекодирования.
        assert base64.b64decode(frames[0][len(_PREFIX) :]) == jpeg
    finally:
        harness.shutdown()
