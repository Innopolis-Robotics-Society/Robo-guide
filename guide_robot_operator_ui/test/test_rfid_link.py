"""Юниты lib/rfid_link.py + RfidBackend -- без rclpy, без реального порта (design E4)."""

from __future__ import annotations

import hashlib
import hmac
import json

from guide_robot_operator_ui.lib.auth import RfidBackend
from guide_robot_operator_ui.lib.rfid_link import MemorySerialPort, RfidLink

SECRET = "shared-secret-from-esp"  # noqa: S105 -- тестовая фикстура, не реальный секрет
NONCE = "test-nonce-64hex-placeholder"


def _signed_response(nonce: str, *, card: str = "op_mook", secret: str = SECRET) -> str:
    resp = hmac.new(secret.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return json.dumps({"ok": True, "resp": resp, "card": card})


# -- RfidLink: протокол ---------------------------------------------------------


def test_challenge_writes_nonce_as_json_line() -> None:
    port = MemorySerialPort()
    port.responses.append(_signed_response(NONCE))
    RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert len(port.written) == 1
    sent = json.loads(port.written[0])
    assert sent == {"cmd": "challenge", "nonce": NONCE}


def test_challenge_ok_response_is_parsed() -> None:
    port = MemorySerialPort()
    port.responses.append(_signed_response(NONCE, card="op_mook"))
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert result.ok is True
    assert result.card == "op_mook"
    assert result.err == ""


def test_challenge_esp_reported_failure_is_not_ok() -> None:
    port = MemorySerialPort()
    port.responses.append(json.dumps({"ok": False, "err": "no_card"}))
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert result.ok is False
    assert result.err == "no_card"


def test_challenge_no_response_is_timeout_not_hang() -> None:
    """design E4, критерий 16: ответ дольше таймаута -- недоступность, не зависание."""
    port = MemorySerialPort()  # пустая очередь -- read_line() сразу вернёт None
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert result.ok is False
    assert result.err == "timeout"


def test_challenge_malformed_json_is_bad_response() -> None:
    port = MemorySerialPort()
    port.responses.append("not json at all")
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert result.ok is False
    assert result.err == "bad_response"


def test_challenge_missing_ok_field_is_bad_response() -> None:
    port = MemorySerialPort()
    port.responses.append(json.dumps({"resp": "abc", "card": "op_mook"}))
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert result.ok is False
    assert result.err == "bad_response"


def test_challenge_result_never_carries_uid_field() -> None:
    """design E4, критерий 17: как бы ESP ни ответил, наружу -- только ok/resp/card/err."""
    port = MemorySerialPort()
    port.responses.append(
        json.dumps({"ok": True, "resp": "abc", "card": "op_mook", "uid": "DEADBEEF1234"})
    )
    result = RfidLink(port, timeout_s=0.1).challenge(NONCE)
    assert vars(result) == {"ok": True, "resp": "abc", "card": "op_mook", "err": ""}


# -- RfidBackend: сверка HMAC ------------------------------------------------------


def test_rfid_backend_accepts_correct_hmac() -> None:
    port = MemorySerialPort()
    port.responses.append(_signed_response(NONCE, card="op_mook"))
    backend = RfidBackend(RfidLink(port, timeout_s=0.1), SECRET)
    result = backend.verify(NONCE, {})
    assert result.ok is True
    assert result.operator == "op_mook"


def test_rfid_backend_rejects_wrong_hmac() -> None:
    """design E4, критерий 16: неверный HMAC -- отказ."""
    port = MemorySerialPort()
    port.responses.append(json.dumps({"ok": True, "resp": "0" * 64, "card": "op_mook"}))
    backend = RfidBackend(RfidLink(port, timeout_s=0.1), SECRET)
    result = backend.verify(NONCE, {})
    assert result.ok is False
    assert result.operator == ""


def test_rfid_backend_rejects_hmac_signed_with_wrong_secret() -> None:
    port = MemorySerialPort()
    port.responses.append(_signed_response(NONCE, secret="a different secret"))
    backend = RfidBackend(RfidLink(port, timeout_s=0.1), SECRET)
    result = backend.verify(NONCE, {})
    assert result.ok is False


def test_rfid_backend_unavailable_on_timeout_falls_through_not_hangs() -> None:
    """design E4, критерий 16: ответ дольше таймаута -- недоступность бэкенда, не зависание."""
    port = MemorySerialPort()
    backend = RfidBackend(RfidLink(port, timeout_s=0.05), SECRET)
    result = backend.verify(NONCE, {})
    assert result.ok is False
    assert result.reason == "rfid_timeout"


def test_rfid_backend_unavailable_without_link() -> None:
    """design E5, критерий 15: нет порта при старте -- бэкенд недоступен, узел поднимается."""
    backend = RfidBackend(None, SECRET)
    assert backend.available() is False
    result = backend.verify(NONCE, {})
    assert result.ok is False
    assert result.reason == "rfid_unavailable"


def test_rfid_backend_unavailable_without_secret() -> None:
    backend = RfidBackend(RfidLink(MemorySerialPort(), timeout_s=0.1), "")
    assert backend.available() is False


def test_rfid_backend_available_with_link_and_secret() -> None:
    backend = RfidBackend(RfidLink(MemorySerialPort(), timeout_s=0.1), SECRET)
    assert backend.available() is True


def test_rfid_backend_missing_card_is_rejected() -> None:
    """`card` пустой -- нечем атрибутировать (design E4)."""
    port = MemorySerialPort()
    resp = hmac.new(SECRET.encode(), NONCE.encode(), hashlib.sha256).hexdigest()
    port.responses.append(json.dumps({"ok": True, "resp": resp, "card": ""}))
    backend = RfidBackend(RfidLink(port, timeout_s=0.1), SECRET)
    result = backend.verify(NONCE, {})
    assert result.ok is False
    assert result.reason == "rfid_missing_card"
