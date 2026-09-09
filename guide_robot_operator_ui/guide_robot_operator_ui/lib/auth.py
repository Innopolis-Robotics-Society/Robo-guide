"""Бэкенды аутентификации оператора -- без rclpy, без aiohttp (design E3).

Один интерфейс (`AuthBackend`), несколько реализаций: PIN -- резерв и
потолок стойкости всей схемы (переживает отказ ридера), RFID -- удобство и
атрибуция в логах, не стойкость (E4, `RfidBackend` появится вместе с ним).
MockBackend -- только для стенда без железа.

`"mock"` в `auth_backends` коротит ВСЮ цепочку, а не участвует в переборе
по порядку: если бы он был рядовой записью, `["pin","mock"]` и
`["mock","pin"]` вели бы себя по-разному, и мок срабатывал бы не при
каждой попытке входа -- худший вид отладочного бэкенда, тот, что не всегда
срабатывает. `AuthChain.verify()` -- единственное место, где это решается.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "AuthBackend",
    "AuthChain",
    "AuthResult",
    "MockBackend",
    "PinBackend",
    "make_auth_chain",
]

_KNOWN_BACKEND_NAMES = frozenset({"pin", "rfid", "mock"})


@dataclass(frozen=True)
class AuthResult:
    """Итог одной попытки verify(). `operator` пусто, если ok=False."""

    ok: bool
    operator: str
    reason: str


class AuthBackend(Protocol):
    """Один способ подтвердить личность оператора."""

    name: str

    def available(self) -> bool:
        """Готов ли бэкенд прямо сейчас (например, открыт ли последовательный порт)."""

    def verify(self, nonce: str, payload: dict[str, Any]) -> AuthResult:
        """Проверить попытку входа по телу POST /api/auth/verify."""


class PinBackend:
    """Сравнение через hmac.compare_digest -- НЕ `==`, не палит содержимое по времени ответа."""

    name = "pin"

    def __init__(self, pin: str) -> None:
        """Запомнить эталонный PIN (уже провалидированный вызывающим на длину)."""
        self._pin = pin

    def available(self) -> bool:
        """PIN не завязан на железо -- доступен всегда."""
        return True

    def verify(self, nonce: str, payload: dict[str, Any]) -> AuthResult:
        """Сверить `payload["pin"]` с эталоном константным по времени сравнением."""
        del nonce
        candidate = payload.get("pin")
        if not isinstance(candidate, str) or not candidate:
            return AuthResult(ok=False, operator="", reason="missing_pin")
        if hmac.compare_digest(candidate, self._pin):
            return AuthResult(ok=True, operator="pin", reason="")
        return AuthResult(ok=False, operator="", reason="wrong_pin")


class MockBackend:
    """Всегда успех -- задействуется только через `AuthChain.mock_active`, см. докстринг модуля."""

    name = "mock"

    def available(self) -> bool:
        """Мок не завязан на железо -- доступен всегда, пока он включён."""
        return True

    def verify(self, nonce: str, payload: dict[str, Any]) -> AuthResult:
        """Всегда успех; операторская атрибуция -- "mock", не имя запрошенного бэкенда."""
        del nonce, payload
        return AuthResult(ok=True, operator="mock", reason="")


@dataclass(frozen=True)
class AuthChain:
    """Собранная и провалидированная на старте цепочка бэкендов."""

    backends: dict[str, AuthBackend]
    order: list[str]
    mock_active: bool

    def available_names(self) -> list[str]:
        """Реальные (не mock) бэкенды, готовые прямо сейчас -- для /api/auth/challenge."""
        return [name for name in self.order if self.backends[name].available()]

    def verify(self, nonce: str, backend_name: str, payload: dict[str, Any]) -> AuthResult:
        """Проверить попытку входа. mock_active коротит это целиком -- см. докстринг модуля."""
        if self.mock_active:
            return AuthResult(ok=True, operator="mock", reason="")
        backend = self.backends.get(backend_name)
        if backend is None:
            return AuthResult(ok=False, operator="", reason="unknown_backend")
        if not backend.available():
            return AuthResult(ok=False, operator="", reason="backend_unavailable")
        return backend.verify(nonce, payload)


def make_auth_chain(
    names: list[str],
    *,
    operator_pin: str,
    rfid_backend: AuthBackend | None = None,
) -> AuthChain:
    """Собрать цепочку по списку имён параметра `auth_backends` (design E3).

    Отказ стартовать, не молчаливое игнорирование: пустой список; "pin"
    отсутствует (потолок стойкости всей схемы обязан быть доступен всегда);
    неизвестное имя; "rfid" указан, но `rfid_backend` не передан (E4 ещё не
    подключён -- до тех пор дефолт `auth_backends` обязан быть `["pin"]`).
    """
    if not names:
        raise ValueError("auth_backends: пустой список -- PIN обязателен")
    if "pin" not in names:
        raise ValueError('auth_backends: "pin" обязателен -- потолок стойкости всей схемы')
    unknown = sorted({name for name in names if name not in _KNOWN_BACKEND_NAMES})
    if unknown:
        raise ValueError(f"auth_backends: неизвестные бэкенды {unknown!r}")
    if "rfid" in names and rfid_backend is None:
        raise ValueError('auth_backends: "rfid" указан, но rfid_backend не передан')

    backends: dict[str, AuthBackend] = {"pin": PinBackend(operator_pin)}
    if rfid_backend is not None:
        backends["rfid"] = rfid_backend
    mock_active = "mock" in names
    if mock_active:
        backends["mock"] = MockBackend()

    # order -- только реальные способы входа для UI (design E2's
    # /api/auth/challenge "backends"); mock туда не попадает, он не
    # выбираемый метод, а глобальный обход всей проверки.
    order = [name for name in names if name in backends and name != "mock"]
    return AuthChain(backends=backends, order=order, mock_active=mock_active)
