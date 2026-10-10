"""Бэкенды аутентификации оператора -- без rclpy, без aiohttp.

Один интерфейс (`AuthBackend`), несколько реализаций: PIN -- резерв и
потолок стойкости всей схемы (переживает отказ ридера), RFID -- удобство и
атрибуция в логах, не стойкость.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol

from .rfid_link import RfidLink

__all__ = [
    "AuthBackend",
    "AuthChain",
    "AuthResult",
    "PinBackend",
    "RfidBackend",
    "make_auth_chain",
]

_KNOWN_BACKEND_NAMES = frozenset({"pin", "rfid"})


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


class RfidBackend:
    """HMAC challenge/response через ESP32-S3/RC522 (design E4).

    Секрет не покидает ESP -- по проводу только подпись (`RfidLink`),
    сверка HMAC -- здесь. `verify()` синхронный и может блокироваться до
    `retries * rfid_timeout_s` (порт открывается один раз при создании, не
    на каждый вызов) -- вызывающий (server.py) обязан звать это
    через `asyncio.to_thread`, не напрямую из event loop сервера.
    """

    name = "rfid"

    def __init__(self, link: RfidLink | None, secret: str, *, retries: int = 5) -> None:
        """`link=None` -- порт не открылся при старте (design E4: узел всё равно поднимается).

        `retries` -- сколько раз опросить мост подряд, пока он отвечает
        "no_card" (firmware/rfid_bridge/src/main.cpp:19-22: один REQA на
        challenge не всегда видит неподвижно лежащую карту, на живом
        железе наблюдалось 3 подряд no_card перед успехом -- одиночный
        опрос это не отказ входа, а промах антиколлизии).
        """
        self._link = link
        self._secret = secret.encode("utf-8")
        self._retries = max(1, retries)

    def available(self) -> bool:
        """Готов, только если порт доступен И секрет загружен -- оба обязательны."""
        if self._link is None or not self._secret:
            return False
        link_available = getattr(self._link, "available", None)
        return bool(link_available()) if callable(link_available) else True

    def verify(self, nonce: str, payload: dict[str, Any]) -> AuthResult:
        """Дёрнуть ридер (с ретраями на no_card) и сверить HMAC-SHA256(secret, nonce)."""
        del payload  # RFID ничего не берёт из тела POST -- решает сам ридер
        if self._link is None or not self._secret:
            return AuthResult(ok=False, operator="", reason="rfid_unavailable")
        challenge = self._link.challenge(nonce)
        for _ in range(self._retries - 1):
            if challenge.ok or challenge.err != "no_card":
                break
            challenge = self._link.challenge(nonce)
        if not challenge.ok:
            return AuthResult(ok=False, operator="", reason=f"rfid_{challenge.err}")
        expected = hmac.new(self._secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(challenge.resp, expected):
            return AuthResult(ok=False, operator="", reason="rfid_bad_signature")
        if not challenge.card:
            return AuthResult(ok=False, operator="", reason="rfid_missing_card")
        # card -- логическое имя оператора (design E4), НЕ UID -- уже
        # гарантировано схемой ChallengeResult в rfid_link.py.
        return AuthResult(ok=True, operator=challenge.card, reason="")


@dataclass(frozen=True)
class AuthChain:
    """Собранная и провалидированная на старте цепочка бэкендов."""

    backends: dict[str, AuthBackend]
    order: list[str]

    def available_names(self) -> list[str]:
        """Бэкенды, готовые прямо сейчас, в порядке конфига -- для /api/auth/challenge."""
        return [name for name in self.order if self.backends[name].available()]

    def verify(self, nonce: str, backend_name: str, payload: dict[str, Any]) -> AuthResult:
        """Проверить попытку входа выбранным бэкендом."""
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
    """Собрать цепочку по списку имён конфига `auth_backends`.

    Отказ стартовать, не молчаливое игнорирование: пустой список; "pin"
    отсутствует (потолок стойкости всей схемы обязан быть доступен всегда);
    неизвестное имя; "rfid" указан, но `rfid_backend` не передан (вызывающий
    обязан собрать `RfidBackend` сам -- отсутствие порта/секрета делает его
    `available() == False`, а не отсутствующим, design E4).
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

    order = [name for name in names if name in backends]
    return AuthChain(backends=backends, order=order)
