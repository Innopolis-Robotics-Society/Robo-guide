"""Юниты lib/auth.py -- без rclpy, без ROS-графа, без железа (design E3)."""

from __future__ import annotations

import pytest
from guide_robot_operator_ui.lib.auth import AuthResult, PinBackend, make_auth_chain


class _StubBackend:
    """RFID-заглушка для тестов make_auth_chain, не пишущая реальный протокол."""

    name = "rfid"

    def __init__(self, ok: bool = True, available: bool = True) -> None:
        self._ok = ok
        self._available = available

    def available(self) -> bool:
        return self._available

    def verify(self, nonce: str, payload: dict) -> AuthResult:
        del nonce, payload
        return AuthResult(ok=self._ok, operator="op_test" if self._ok else "", reason="")


# -- PinBackend ----------------------------------------------------------------


def test_pin_backend_accepts_matching_pin() -> None:
    backend = PinBackend("changeme")
    result = backend.verify("nonce", {"pin": "changeme"})
    assert result.ok is True
    assert result.operator == "pin"


def test_pin_backend_rejects_wrong_pin() -> None:
    backend = PinBackend("changeme")
    result = backend.verify("nonce", {"pin": "wrong"})
    assert result.ok is False
    assert result.operator == ""


def test_pin_backend_rejects_missing_pin() -> None:
    backend = PinBackend("changeme")
    result = backend.verify("nonce", {})
    assert result.ok is False
    assert result.reason == "missing_pin"


def test_pin_backend_always_available() -> None:
    assert PinBackend("changeme").available() is True


# -- make_auth_chain: проверки при старте (design E3, критерии 9/10) -----------


def test_empty_backends_list_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="пустой список"):
        make_auth_chain([], operator_pin="changeme")


def test_missing_pin_refuses_to_start() -> None:
    with pytest.raises(ValueError, match='"pin" обязателен'):
        make_auth_chain(["mock"], operator_pin="changeme")


def test_unknown_backend_name_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="неизвестные бэкенды"):
        make_auth_chain(["pin", "quantum"], operator_pin="changeme")


def test_rfid_without_rfid_backend_refuses_to_start() -> None:
    """E4 ещё не подключён -- "rfid" без rfid_backend не молча игнорируется."""
    with pytest.raises(ValueError, match="rfid_backend не передан"):
        make_auth_chain(["rfid", "pin"], operator_pin="changeme")


def test_rfid_with_rfid_backend_starts() -> None:
    chain = make_auth_chain(["rfid", "pin"], operator_pin="changeme", rfid_backend=_StubBackend())
    assert chain.available_names() == ["rfid", "pin"]


# -- AuthChain.verify(): mock коротит цепочку целиком (пользовательская правка) --


def test_mock_present_succeeds_regardless_of_requested_backend_and_payload() -> None:
    chain = make_auth_chain(["mock", "pin"], operator_pin="changeme")
    result = chain.verify("nonce", "pin", {"pin": "totally wrong"})
    assert result.ok is True
    assert result.operator == "mock"


def test_mock_present_at_any_position_still_shortcuts() -> None:
    """["pin","mock"] и ["mock","pin"] обязаны вести себя одинаково."""
    chain_a = make_auth_chain(["pin", "mock"], operator_pin="changeme")
    chain_b = make_auth_chain(["mock", "pin"], operator_pin="changeme")
    for chain in (chain_a, chain_b):
        result = chain.verify("nonce", "pin", {"pin": "wrong"})
        assert result.ok is True
        assert result.operator == "mock"


def test_mock_not_in_available_names() -> None:
    """mock не выбираемый метод на экране входа -- глобальный обход, не опция."""
    chain = make_auth_chain(["mock", "pin"], operator_pin="changeme")
    assert "mock" not in chain.available_names()


def test_mock_active_flag() -> None:
    assert make_auth_chain(["pin"], operator_pin="changeme").mock_active is False
    assert make_auth_chain(["mock", "pin"], operator_pin="changeme").mock_active is True


# -- AuthChain.verify(): без mock -----------------------------------------------


def test_verify_dispatches_to_named_backend() -> None:
    chain = make_auth_chain(["rfid", "pin"], operator_pin="changeme", rfid_backend=_StubBackend())
    result = chain.verify("nonce", "pin", {"pin": "changeme"})
    assert result.ok is True
    assert result.operator == "pin"


def test_verify_unknown_backend_name_rejected() -> None:
    chain = make_auth_chain(["pin"], operator_pin="changeme")
    result = chain.verify("nonce", "rfid", {})
    assert result.ok is False
    assert result.reason == "unknown_backend"


def test_verify_unavailable_backend_rejected() -> None:
    stub = _StubBackend(available=False)
    chain = make_auth_chain(["rfid", "pin"], operator_pin="changeme", rfid_backend=stub)
    result = chain.verify("nonce", "rfid", {})
    assert result.ok is False
    assert result.reason == "backend_unavailable"


def test_available_names_excludes_unavailable_backend() -> None:
    stub = _StubBackend(available=False)
    chain = make_auth_chain(["rfid", "pin"], operator_pin="changeme", rfid_backend=stub)
    assert chain.available_names() == ["pin"]
