"""Роуты входа, текущего тура, команд оператора, стека и публичного старта."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_launcher.api import (
    API,
    COOKIE,
    MAX_FAILED_ATTEMPTS,
    PUBLIC_CONFIRM_MIN_INTERVAL_S,
    PUBLIC_STOP_MIN_INTERVAL_S,
)
from guide_launcher.auth import RfidBackend, make_auth_chain
from guide_launcher.bridge import FRAME_MAX_AGE_S, Bridge, Frame, FrameWatcher
from guide_launcher.rfid_link import MemorySerialPort, RfidLink
from guide_launcher.server import create_app
from guide_launcher.stack import StackMonitor, StackState
from helpers import Clock, FakeDocker, make_cfg

PIN = "12345678"
SECRET = "rfid-secret"  # noqa: S105 -- тестовая фикстура
TOURS = {"tours": [{"id": "expo_one", "name": "Expo"}, {"id": "expo_two", "name": "Two"}]}

AUTHED_ROUTES = [
    ("POST", "/api/v1/reference/preferences"),
    ("POST", "/api/v1/reference/preferences"),
    ("POST", "/api/v1/stack/start"),
    ("POST", "/api/v1/stack/restart"),
    ("GET", "/api/v1/stack/log"),
    ("POST", "/api/v1/missions"),
    ("POST", "/api/v1/missions/stop"),
    ("POST", "/api/v1/missions/return"),
    ("POST", "/api/v1/missions/base"),
    ("POST", "/api/v1/missions/redirect"),
    ("POST", "/api/v1/localization/reset"),
    ("POST", "/api/v1/costmaps/clear"),
]


def run(coro):
    return asyncio.run(coro)


def make_backend() -> web.Application:
    calls: list[dict[str, Any]] = []
    app = web.Application()
    app["calls"] = calls
    app["statuses"] = {}

    async def tours(request):
        return web.json_response(TOURS)

    def command(path: str):
        async def handler(request):
            body = await request.json() if request.can_read_body else None
            calls.append({"path": path, "headers": dict(request.headers), "body": body})
            status = app["statuses"].get(path, 200)
            return web.json_response({"ok": status < 400, "path": path}, status=status)

        return handler

    app.router.add_get("/healthz", tours)
    app.router.add_get("/internal/tours", tours)
    for path in (
        "/internal/speech",
        "/internal/missions",
        "/internal/missions/stop",
        "/internal/missions/return",
        "/internal/missions/base",
        "/internal/localization/reset",
        "/internal/costmaps/clear",
        "/internal/interaction/confirm",
    ):
        app.router.add_post(path, command(path))
    return app


@dataclass
class Env:
    client: TestClient
    backend: TestServer
    monitor: StackMonitor
    docker: FakeDocker
    clock: Clock
    watcher: FrameWatcher
    app: web.Application

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.backend.app["calls"]

    def set_frame(self, **over: Any) -> None:
        data = {
            "readiness": {"mission": True, "speech": True},
            "bridge_instance_id": "test-bridge",
            "state_name": "idle",
            "estop": False,
            "supervisor_state": "ACTIVE",
            "mission_state_age_s": 0.5,
            **over,
        }
        self.watcher.frame = Frame(data, self.clock.t)

    def advance(self, seconds: float) -> None:
        """Сдвинуть часы; живой мост шлёт heartbeat, поэтому кадр перештамповывается."""
        self.clock.t += seconds
        if self.watcher.frame is not None:
            self.watcher.frame = Frame(self.watcher.frame.data, self.clock.t)

    def journal(self) -> list[dict[str, Any]]:
        path = self.app[API].journal.path
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    async def login(self, pin: str = PIN) -> Any:
        nonce = (await (await self.client.post("/api/v1/auth/challenge")).json())["nonce"]
        body = {"nonce": nonce, "backend": "pin", "pin": pin}
        return await self.client.post("/api/v1/auth/verify", json=body)


@contextlib.asynccontextmanager
async def env(tmp_path: Path, *, up: bool = True, auth=None, **cfg_kw):
    backend = TestServer(make_backend())
    await backend.start_server()
    cfg = make_cfg(tmp_path, **cfg_kw)
    bridge = Bridge(str(backend.make_url("")), "tok")
    docker, clock = FakeDocker(), Clock()
    monitor = StackMonitor(cfg, bridge.probe, run=docker, clock=clock, sleep=clock.sleep)
    monitor.state = StackState.UP if up else StackState.DOWN
    watcher = FrameWatcher(bridge, lambda: True, clock=clock)
    app = create_app(
        cfg,
        monitor=monitor,
        bridge=bridge,
        watcher=watcher,
        run_background=False,
        auth=auth,
        clock=clock,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield Env(client, backend, monitor, docker, clock, watcher, app)
    finally:
        await client.close()
        await backend.close()


# -- вход и сессия -----------------------------------------------------------------


def test_authed_routes_return_401_without_session(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            for method, path in AUTHED_ROUTES:
                resp = await e.client.request(method, path, json={})
                assert resp.status == 401, (method, path)
                assert (await resp.json())["error"]["message"] == "auth_required"
            assert e.calls == []
            assert e.docker.calls == []

    run(go())


def test_login_sets_httponly_strict_cookie_and_status(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            resp = await e.login()
            assert resp.status == 200
            body = await resp.json()
            assert body["operator"] == "pin"
            assert "token" not in body
            cookie = resp.headers["Set-Cookie"]
            assert cookie.startswith(f"{COOKIE}=")
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie

            status = await (await e.client.get("/api/v1/auth/status")).json()
            assert status["active"] is True and status["operator"] == "pin"

    run(go())


def test_wrong_pin_bad_nonce_and_lockout(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            resp = await e.client.post(
                "/api/v1/auth/verify", json={"nonce": "nope", "backend": "pin", "pin": PIN}
            )
            assert (
                resp.status == 401 and (await resp.json())["error"]["message"] == "invalid_nonce"
            )

            resp = await e.client.post("/api/v1/auth/verify", json={"backend": "pin"})
            assert resp.status == 400

            for _ in range(MAX_FAILED_ATTEMPTS):
                resp = await e.login("00000000")
                assert resp.status == 401
                assert (await resp.json())["error"]["message"] == "invalid_credentials"
            resp = await e.login(PIN)
            assert resp.status == 429
            assert (await resp.json())["error"]["message"] == "locked_out"
            assert (await e.client.get("/api/v1/auth/status")).status == 200
            status = await (await e.client.get("/api/v1/auth/status")).json()
            assert status["active"] is False

    run(go())


def test_session_expires_and_slides_only_on_successful_posts(tmp_path):
    async def go():
        async with env(tmp_path, session_ttl_s=600.0) as e:
            await e.login()
            e.clock.t += 400
            # GET и отказ (400) окно не продлевают
            assert (await e.client.get("/api/v1/stack/log")).status == 200
            bad = await e.client.post("/api/v1/reference/preferences", json={"tour_id": "bad id"})
            assert bad.status == 400
            e.clock.t += 250
            assert (await e.client.get("/api/v1/stack/log")).status == 401

            await e.login()
            e.clock.t += 400
            ok = await e.client.post("/api/v1/reference/preferences", json={"tour_id": "expo_two"})
            assert ok.status == 200
            e.clock.t += 400
            assert (await e.client.get("/api/v1/stack/log")).status == 200
            e.clock.t += 601
            assert (await e.client.get("/api/v1/stack/log")).status == 401

    run(go())


def test_logout_closes_session_and_clears_cookie(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            resp = await e.client.post("/api/v1/auth/logout")
            assert resp.status == 200
            assert COOKIE in resp.headers["Set-Cookie"]
            assert (await e.client.get("/api/v1/stack/log")).status == 401

    run(go())


def _signed(nonce: str, card: str = "op_mook") -> str:
    resp = hmac.new(SECRET.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"ok": True, "resp": resp, "card": card})


def test_rfid_login_via_memory_port(tmp_path):
    async def go():
        port = MemorySerialPort()
        rfid = RfidBackend(RfidLink(port, timeout_s=0.01), SECRET)
        chain = make_auth_chain(["rfid", "pin"], operator_pin=PIN, rfid_backend=rfid)
        async with env(tmp_path, auth=chain) as e:
            chal = await (await e.client.post("/api/v1/auth/challenge")).json()
            assert chal["backends"] == ["rfid", "pin"]
            port.responses.append(_signed(chal["nonce"]))
            resp = await e.client.post(
                "/api/v1/auth/verify", json={"nonce": chal["nonce"], "backend": "rfid"}
            )
            assert resp.status == 200 and (await resp.json())["operator"] == "op_mook"

            chal = await (await e.client.post("/api/v1/auth/challenge")).json()
            resp = await e.client.post(
                "/api/v1/auth/verify", json={"nonce": chal["nonce"], "backend": "rfid"}
            )
            assert resp.status == 401
            assert (await resp.json()).get("reason") is None
            events = [(r["event"], r.get("ok"), r.get("operator")) for r in e.journal()]
            assert ("auth_attempt", True, "op_mook") in events

    run(go())


# -- текущий тур -------------------------------------------------------------------


# -- команды оператора -------------------------------------------------------------


# -- публичный старт ---------------------------------------------------------------


async def _public(e: Env, **body: Any):
    return await e.client.post(
        "/api/v1/public/start-tour",
        json={"tour_id": "expo_one", **body},
        headers={"Idempotency-Key": "public-start"},
    )


# -- публичный ответ на «Идём дальше?» ------------------------------------------------


async def _confirm(e: Env, answer: Any = "yes"):
    return await e.client.post("/api/v1/public/confirm", json={"answer": answer})


@pytest.mark.parametrize("answer", ["yes", "stop", "question"])
def test_public_confirm_forwards_answer_without_session(tmp_path, answer):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name="awaiting_confirm")
            resp = await _confirm(e, answer)
            assert resp.status == 200
            data = await resp.json()
            assert {key: data[key] for key in ("ok", "answer", "listening")} == {
                "ok": True,
                "answer": answer,
                "listening": False,
            }
            assert data["accepted"] is True and data["state"]["data_fresh"]
            (call,) = e.calls
            assert call["path"] == "/internal/interaction/confirm"
            assert call["body"] == {"answer": answer}
            assert call["headers"]["X-Operator"] == "public"
            (entry,) = [r for r in e.journal() if r.get("event") == "public_confirm"]
            assert entry["answer"] == answer and entry["ok"] is True

    run(go())


@pytest.mark.parametrize("answer", ["no", "", None, 1])
def test_public_confirm_rejects_unknown_answer(tmp_path, answer):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name="awaiting_confirm")
            resp = await _confirm(e, answer)
            assert resp.status == 400
            assert e.calls == []

    run(go())


def test_public_confirm_refusals(tmp_path):
    async def go():
        async with env(tmp_path) as e:

            async def refused(reason: str) -> None:
                e.advance(PUBLIC_CONFIRM_MIN_INTERVAL_S + 1)
                resp = await _confirm(e)
                assert resp.status == 409, reason
                assert (await resp.json())["error"]["message"] == reason

            e.monitor.state = StackState.DOWN
            e.set_frame(state_name="awaiting_confirm")
            await refused("ros_down")
            e.monitor.state = StackState.UP

            e.watcher.frame = None
            await refused("no_mission_fsm")
            e.set_frame(state_name="awaiting_confirm", mission_state_age_s=5.0)
            await refused("no_mission_fsm")
            e.set_frame(state_name="awaiting_confirm", estop=True)
            await refused("estop")
            for state in ("idle", "narrating", "answering", "navigating"):
                e.set_frame(state_name=state)
                await refused("not_awaiting_confirm")
            assert e.calls == []

            e.set_frame(state_name="awaiting_confirm")
            e.backend.app["statuses"]["/internal/interaction/confirm"] = 409
            await refused("upstream_409")

    run(go())


def test_public_confirm_disabled_in_config(tmp_path):
    async def go():
        async with env(tmp_path, visitor_confirm_buttons=False) as e:
            e.set_frame(state_name="awaiting_confirm")
            resp = await _confirm(e)
            assert resp.status == 409
            assert (await resp.json())["error"]["message"] == "disabled"
            assert e.calls == []

    run(go())


def test_public_confirm_double_tap_is_rate_limited(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name="awaiting_confirm")
            assert (await _confirm(e)).status == 200
            resp = await _confirm(e, "stop")
            assert (await resp.json())["error"]["message"] == "rate_limited"
            assert len(e.calls) == 1

    run(go())


# -- публичный «Стоп» во время движения --------------------------------------------


async def _stop(e: Env):
    return await e.client.post("/api/v1/public/stop")


@pytest.mark.parametrize("state", ["navigating", "returning"])
def test_public_stop_forwards_without_session(tmp_path, state):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name=state, estop=True)  # E-STOP стопу не помеха
            resp = await _stop(e)
            assert resp.status == 200
            (call,) = e.calls
            assert call["path"] == "/internal/missions/stop"
            assert call["headers"]["X-Operator"] == "public"
            (entry,) = [r for r in e.journal() if r.get("event") == "public_stop"]
            assert entry["ok"] is True

    run(go())


def test_public_stop_refusals(tmp_path):
    async def go():
        async with env(tmp_path) as e:

            async def refused(reason: str) -> None:
                e.advance(PUBLIC_STOP_MIN_INTERVAL_S + 1)
                resp = await _stop(e)
                assert resp.status == 409, reason
                assert (await resp.json())["error"]["message"] == reason

            e.monitor.state = StackState.DOWN
            e.set_frame(state_name="navigating")
            await refused("ros_down")
            e.monitor.state = StackState.UP

            e.watcher.frame = None
            await refused("no_mission_fsm")
            for state in ("idle", "narrating", "awaiting_confirm", "paused"):
                e.set_frame(state_name=state)
                await refused("not_moving")
            assert e.calls == []

            e.set_frame(state_name="navigating")
            e.advance(PUBLIC_STOP_MIN_INTERVAL_S + 1)
            assert (await _stop(e)).status == 200
            resp = await _stop(e)
            assert (await resp.json())["error"]["message"] == "rate_limited"
            assert len(e.calls) == 1

    run(go())


# -- стек --------------------------------------------------------------------------


async def _settle(e: Env) -> None:
    await asyncio.gather(*list(e.app[API]._tasks))


def test_restart_refused_while_tour_active(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame(state_name="navigating")
            resp = await e.client.post("/api/v1/stack/restart")
            assert resp.status == 409
            assert (await resp.json())["error"]["message"] == "robot_moving"
            assert e.docker.calls == []

    run(go())


def test_stack_start_and_log_and_disabled_control(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            await e.login()
            resp = await e.client.post("/api/v1/stack/start")
            assert resp.status == 202
            await _settle(e)
            assert "exec:-d" in e.docker.verbs()
            log = await (await e.client.get("/api/v1/stack/log")).json()
            assert log == {"lines": ["line1", "line2"]}
        (tmp_path / "off").mkdir()
        async with env(tmp_path / "off", up=False, container="") as e:
            await e.login()
            resp = await e.client.post("/api/v1/stack/start")
            assert resp.status == 400
            assert (await resp.json())["error"]["message"] == "stack_control_disabled"

    run(go())


# -- журнал ------------------------------------------------------------------------


def test_silent_bridge_frame_expires_for_restart_and_public_start(tmp_path):
    async def go():
        async with env(tmp_path, default_tour="expo_one") as e:
            await e.login()
            e.set_frame(state_name="navigating")
            assert e.watcher.latest() is not None
            e.clock.t += FRAME_MAX_AGE_S - 0.1
            assert e.watcher.latest() is not None
            e.clock.t += 0.2
            assert e.watcher.latest() is None

            resp = await _public(e)
            assert resp.status == 403
            assert (await resp.json())["error"]["message"] == "operator_session_required"

            e.set_frame(state_name="navigating")
            assert (await e.client.post("/api/v1/stack/restart")).status == 409
            e.set_frame(state_name="navigating")
            e.clock.t += FRAME_MAX_AGE_S + 1
            e.docker.calls.clear()
            e.monitor._starting_since = None
            assert (await e.client.post("/api/v1/stack/restart")).status == 409
            assert "restart" not in e.docker.verbs()

    run(go())


def test_rfid_empty_reader_polling_does_not_lock_out_login(tmp_path):
    async def go():
        port = MemorySerialPort()
        rfid = RfidBackend(RfidLink(port, timeout_s=0.01), SECRET, retries=1)
        chain = make_auth_chain(["rfid", "pin"], operator_pin=PIN, rfid_backend=rfid)
        async with env(tmp_path, auth=chain) as e:
            for _ in range(65):
                chal = (await (await e.client.post("/api/v1/auth/challenge")).json())
                port.responses.append(json.dumps({"ok": False, "err": "no_card"}))
                response = await e.client.post("/api/v1/auth/verify", json={
                    "nonce": chal["nonce"], "backend": "rfid"})
                assert response.status == 401
                assert (await response.json())["error"]["message"] == "rfid_no_card"
            chal = (await (await e.client.post("/api/v1/auth/challenge")).json())
            port.responses.append(_signed(chal["nonce"]))
            response = await e.client.post("/api/v1/auth/verify", json={
                "nonce": chal["nonce"], "backend": "rfid"})
            assert response.status == 200
    run(go())
