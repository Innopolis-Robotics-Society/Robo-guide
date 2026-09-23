"""Юниты lib/session.py -- без rclpy, без sleep (часы инжектируются, design E2)."""

from __future__ import annotations

from guide_robot_operator_ui.lib.session import SessionManager


class _FakeClock:
    """Управляемые часы: monotonic и wall движутся вместе одним `advance()`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _new_manager(
    clock: _FakeClock,
    *,
    session_ttl_s: float = 600.0,
    nonce_ttl_s: float = 30.0,
    max_failed_attempts: int = 5,
    lockout_s: float = 60.0,
) -> SessionManager:
    return SessionManager(
        session_ttl_s=session_ttl_s,
        nonce_ttl_s=nonce_ttl_s,
        max_failed_attempts=max_failed_attempts,
        lockout_s=lockout_s,
        time_fn=clock,
        wall_time_fn=clock,
    )


# -- nonce -----------------------------------------------------------------------


def test_nonce_is_valid_once() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock)
    nonce = mgr.issue_nonce()
    assert mgr.consume_nonce(nonce) is True


def test_nonce_reuse_after_success_fails() -> None:
    """design E2, критерий 5: повторное использование nonce -> невалиден."""
    clock = _FakeClock()
    mgr = _new_manager(clock)
    nonce = mgr.issue_nonce()
    assert mgr.consume_nonce(nonce) is True
    assert mgr.consume_nonce(nonce) is False


def test_nonce_reuse_after_failed_consume_also_fails() -> None:
    """Погашен НЕЗАВИСИМО от исхода -- не только после успеха."""
    clock = _FakeClock()
    mgr = _new_manager(clock, nonce_ttl_s=1.0)
    nonce = mgr.issue_nonce()
    clock.advance(2.0)  # протух
    assert mgr.consume_nonce(nonce) is False
    clock.advance(-2.0)  # даже если бы он снова был "свежим" по времени
    assert mgr.consume_nonce(nonce) is False  # его уже нет в реестре


def test_expired_nonce_is_invalid() -> None:
    """design E2, критерий 6: nonce старше nonce_ttl_s -> невалиден."""
    clock = _FakeClock()
    mgr = _new_manager(clock, nonce_ttl_s=30.0)
    nonce = mgr.issue_nonce()
    clock.advance(30.1)
    assert mgr.consume_nonce(nonce) is False


def test_unknown_nonce_is_invalid() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock)
    assert mgr.consume_nonce("never-issued") is False


# -- сессия и скользящий TTL -------------------------------------------------------


def test_session_valid_immediately_after_creation() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock)
    info, evicted = mgr.create_session(operator="pin", backend="pin")
    assert evicted is None
    assert mgr.validate(info.token) is not None


def test_session_expires_after_ttl_without_activity() -> None:
    """design E2, критерий 7: токен через session_ttl_s+1 без активности -> невалиден."""
    clock = _FakeClock()
    mgr = _new_manager(clock, session_ttl_s=600.0)
    info, _ = mgr.create_session(operator="pin", backend="pin")
    clock.advance(601.0)
    assert mgr.validate(info.token) is None


def test_touch_extends_window_by_full_ttl_from_now() -> None:
    """Команда за секунду до истечения продлевает окно ещё на полный TTL."""
    clock = _FakeClock()
    mgr = _new_manager(clock, session_ttl_s=600.0)
    info, _ = mgr.create_session(operator="pin", backend="pin")
    clock.advance(599.0)
    mgr.touch(info.token)
    clock.advance(599.0)  # 599+599 > 600, но touch() перезапустил окно
    assert mgr.validate(info.token) is not None
    clock.advance(2.0)  # теперь точно за пределами продлённого окна
    assert mgr.validate(info.token) is None


def test_validate_does_not_extend_window() -> None:
    """validate() -- гейт на входе, БЕЗ побочного продления (design E2)."""
    clock = _FakeClock()
    mgr = _new_manager(clock, session_ttl_s=600.0)
    info, _ = mgr.create_session(operator="pin", backend="pin")
    clock.advance(599.0)
    mgr.validate(info.token)
    clock.advance(2.0)
    assert mgr.validate(info.token) is None


def test_second_login_evicts_first() -> None:
    """Одна активная сессия на узел -- второй успешный вход вытесняет первый."""
    clock = _FakeClock()
    mgr = _new_manager(clock)
    first, evicted_first = mgr.create_session(operator="pin", backend="pin")
    assert evicted_first is None
    second, evicted_second = mgr.create_session(operator="op_mook", backend="rfid")
    assert evicted_second is not None
    assert evicted_second.operator == "pin"
    assert mgr.validate(first.token) is None
    assert mgr.validate(second.token) is not None


def test_logout_clears_session_and_returns_it() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock)
    info, _ = mgr.create_session(operator="pin", backend="pin")
    logged_out = mgr.logout()
    assert logged_out is not None
    assert logged_out.token == info.token
    assert mgr.validate(info.token) is None
    assert mgr.logout() is None  # второй вызов -- нечего закрывать


def test_status_reflects_current_session() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock)
    assert mgr.status() is None
    info, _ = mgr.create_session(operator="pin", backend="pin")
    status = mgr.status()
    assert status is not None
    assert status.token == info.token


def test_wall_clock_jump_does_not_extend_session() -> None:
    """Скачок системных часов не должен продлевать/обрывать сессию (монотонные внутри)."""
    mono = _FakeClock(start=1000.0)
    wall = _FakeClock(start=2_000_000.0)
    mgr = SessionManager(
        session_ttl_s=600.0,
        nonce_ttl_s=30.0,
        max_failed_attempts=5,
        lockout_s=60.0,
        time_fn=mono,
        wall_time_fn=wall,
    )
    info, _ = mgr.create_session(operator="pin", backend="pin")
    wall.value -= 10_000.0  # NTP скачок назад по стенным часам
    mono.advance(601.0)  # но монотонные часы всё равно идут вперёд
    assert mgr.validate(info.token) is None


# -- локаут перебора (design E2, критерий 8) ---------------------------------------


def test_lockout_after_max_failed_attempts() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock, max_failed_attempts=5, lockout_s=60.0)
    assert mgr.lockout_remaining_s() is None
    for _ in range(5):
        mgr.record_failure()
    remaining = mgr.lockout_remaining_s()
    assert remaining is not None
    assert 59.0 < remaining <= 60.0


def test_lockout_expires_after_lockout_s() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock, max_failed_attempts=5, lockout_s=60.0)
    for _ in range(5):
        mgr.record_failure()
    clock.advance(60.1)
    assert mgr.lockout_remaining_s() is None


def test_success_resets_failure_counter() -> None:
    clock = _FakeClock()
    mgr = _new_manager(clock, max_failed_attempts=5, lockout_s=60.0)
    for _ in range(4):
        mgr.record_failure()
    mgr.create_session(operator="pin", backend="pin")  # успех сбрасывает счётчик
    for _ in range(4):
        mgr.record_failure()
    assert mgr.lockout_remaining_s() is None  # 4 < 5, ещё не заблокировано
