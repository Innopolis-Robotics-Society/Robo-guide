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

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_launcher.api import API, COOKIE, MAX_FAILED_ATTEMPTS, PUBLIC_START_MIN_INTERVAL_S
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
    ("POST", "/api/tour/current"),
    ("POST", "/api/stack/start"),
    ("POST", "/api/stack/restart"),
    ("GET", "/api/stack/log"),
    ("POST", "/api/op/tour/start"),
    ("POST", "/api/op/tour/stop"),
    ("POST", "/api/op/go_home"),
    ("POST", "/api/op/localization/reset"),
    ("POST", "/api/op/costmaps/clear"),
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

    app.router.add_get("/api/tours", tours)
    for path in (
        "/api/tour/start",
        "/api/tour/stop",
        "/api/go_home",
        "/api/localization/reset",
        "/api/costmaps/clear",
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
        nonce = (await (await self.client.post("/api/auth/challenge")).json())["nonce"]
        body = {"nonce": nonce, "backend": "pin", "pin": pin}
        return await self.client.post("/api/auth/verify", json=body)


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
                assert await resp.json() == {"error": "auth_required"}
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

            status = await (await e.client.get("/api/auth/status")).json()
            assert status["active"] is True and status["operator"] == "pin"

    run(go())


def test_wrong_pin_bad_nonce_and_lockout(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            resp = await e.client.post(
                "/api/auth/verify", json={"nonce": "nope", "backend": "pin", "pin": PIN}
            )
            assert resp.status == 401 and (await resp.json())["error"] == "invalid_nonce"

            resp = await e.client.post("/api/auth/verify", json={"backend": "pin"})
            assert resp.status == 400

            for _ in range(MAX_FAILED_ATTEMPTS):
                resp = await e.login("00000000")
                assert resp.status == 401
                assert (await resp.json())["error"] == "invalid_credentials"
            resp = await e.login(PIN)
            assert resp.status == 429
            assert (await resp.json())["error"] == "locked_out"
            assert (await e.client.get("/api/auth/status")).status == 200
            status = await (await e.client.get("/api/auth/status")).json()
            assert status["active"] is False

    run(go())


def test_session_expires_and_slides_only_on_successful_posts(tmp_path):
    async def go():
        async with env(tmp_path, session_ttl_s=600.0) as e:
            await e.login()
            e.clock.t += 400
            # GET и отказ (400) окно не продлевают
            assert (await e.client.get("/api/stack/log")).status == 200
            bad = await e.client.post("/api/tour/current", json={"tour_id": "bad id"})
            assert bad.status == 400
            e.clock.t += 250
            assert (await e.client.get("/api/stack/log")).status == 401

            await e.login()
            e.clock.t += 400
            ok = await e.client.post("/api/tour/current", json={"tour_id": "expo_two"})
            assert ok.status == 200
            e.clock.t += 400
            assert (await e.client.get("/api/stack/log")).status == 200
            e.clock.t += 601
            assert (await e.client.get("/api/stack/log")).status == 401

    run(go())


def test_logout_closes_session_and_clears_cookie(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            resp = await e.client.post("/api/auth/logout")
            assert resp.status == 200
            assert COOKIE in resp.headers["Set-Cookie"]
            assert (await e.client.get("/api/stack/log")).status == 401

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
            chal = await (await e.client.post("/api/auth/challenge")).json()
            assert chal["backends"] == ["rfid", "pin"]
            port.responses.append(_signed(chal["nonce"]))
            resp = await e.client.post(
                "/api/auth/verify", json={"nonce": chal["nonce"], "backend": "rfid"}
            )
            assert resp.status == 200 and (await resp.json())["operator"] == "op_mook"

            chal = await (await e.client.post("/api/auth/challenge")).json()
            resp = await e.client.post(
                "/api/auth/verify", json={"nonce": chal["nonce"], "backend": "rfid"}
            )
            assert resp.status == 401
            assert (await resp.json()).get("reason") is None
            events = [(r["event"], r.get("ok"), r.get("operator")) for r in e.journal()]
            assert ("auth_attempt", True, "op_mook") in events

    run(go())


# -- текущий тур -------------------------------------------------------------------


def test_current_tour_default_set_and_persisted(tmp_path):
    async def go():
        async with env(tmp_path, default_tour="expo_one") as e:
            assert await (await e.client.get("/api/tour/current")).json() == {
                "tour_id": "expo_one",
                "source": "default",
            }
            await e.login()
            for bad in ("", "a b", "x" * 65, "../etc", 5, None):
                resp = await e.client.post("/api/tour/current", json={"tour_id": bad})
                assert resp.status == 400, bad
            resp = await e.client.post("/api/tour/current", json={"tour_id": "expo_two"})
            assert await resp.json() == {"tour_id": "expo_two", "source": "state"}
        async with env(tmp_path, default_tour="expo_one") as e2:
            assert await (await e2.client.get("/api/tour/current")).json() == {
                "tour_id": "expo_two",
                "source": "state",
            }

    run(go())


# -- команды оператора -------------------------------------------------------------


def test_op_routes_need_ros_up(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            await e.login()
            for _, path in AUTHED_ROUTES[4:]:
                resp = await e.client.post(path, json={})
                assert resp.status == 409, path
                assert await resp.json() == {"error": "ros_down"}
            assert e.calls == []

    run(go())


def test_op_tour_start_uses_current_tour_and_ignores_client_body(tmp_path):
    async def go():
        async with env(tmp_path, default_tour="expo_one") as e:
            await e.login()
            await e.client.post("/api/tour/current", json={"tour_id": "expo_two"})
            resp = await e.client.post(
                "/api/op/tour/start", json={"tour_id": "evil", "greet": False}
            )
            assert resp.status == 200
            (call,) = e.calls
            assert call["path"] == "/api/tour/start"
            assert call["body"] == {"tour_id": "expo_two"}
            assert call["headers"]["X-Operator"] == "pin"
            assert call["headers"]["X-Bridge-Token"] == "tok"

    run(go())


def test_op_routes_forward_and_pass_status_through(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.backend.app["statuses"]["/api/costmaps/clear"] = 503
            await e.login()
            assert (await e.client.post("/api/op/tour/stop")).status == 200
            assert (await e.client.post("/api/op/go_home")).status == 200
            assert (await e.client.post("/api/op/localization/reset", json={})).status == 200
            resp = await e.client.post("/api/op/costmaps/clear")
            assert resp.status == 503
            assert [c["path"] for c in e.calls] == [
                "/api/tour/stop",
                "/api/go_home",
                "/api/localization/reset",
                "/api/costmaps/clear",
            ]
            assert e.calls[2]["body"] == {"confirm": True}

    run(go())


# -- публичный старт ---------------------------------------------------------------


async def _public(e: Env, **body: Any):
    return await e.client.post("/api/public/start_tour", json=body)


def test_public_start_success_ignores_client_tour_and_needs_no_session(tmp_path):
    async def go():
        async with env(tmp_path, default_tour="expo_two") as e:
            e.set_frame()
            resp = await _public(e, tour_id="expo_one")
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "tour_id": "expo_two"}
            (call,) = e.calls
            assert call["path"] == "/api/tour/start"
            assert call["body"] == {"tour_id": "expo_two"}
            assert call["headers"]["X-Operator"] == "public"

    run(go())


def test_public_start_refusals(tmp_path):
    async def go():
        async with env(tmp_path, default_tour="expo_one") as e:

            async def refused(reason: str) -> None:
                e.advance(PUBLIC_START_MIN_INTERVAL_S + 1)
                resp = await _public(e)
                assert resp.status == 409, reason
                assert await resp.json() == {"error": "refused", "reason": reason}

            e.monitor.state = StackState.DOWN
            e.set_frame()
            await refused("ros_down")
            e.monitor.state = StackState.UP

            e.watcher.frame = None
            await refused("no_mission_fsm")
            e.set_frame(mission_state_age_s=None)
            await refused("no_mission_fsm")
            e.set_frame(mission_state_age_s=5.0)
            await refused("no_mission_fsm")

            e.set_frame(estop=True)
            await refused("estop")
            e.set_frame(supervisor_state="FAULT")
            await refused("estop")

            e.set_frame(state_name="narrating")
            await refused("tour_active")

            e.set_frame()
            e.app[API].tour.set("ghost")
            await refused("unknown_tour")
            assert e.calls == []

            e.backend.app["statuses"]["/api/tour/start"] = 409
            e.app[API].tour.set("expo_one")
            await refused("upstream_409")

    run(go())


def test_public_start_rate_limited(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame()
            assert (await _public(e)).status == 200
            e.advance(PUBLIC_START_MIN_INTERVAL_S - 1)
            resp = await _public(e)
            assert resp.status == 409
            assert (await resp.json())["reason"] == "rate_limited"
            e.advance(2)
            assert (await _public(e)).status == 200
            assert len(e.calls) == 2

    run(go())


# -- стек --------------------------------------------------------------------------


async def _settle(e: Env) -> None:
    await asyncio.gather(*list(e.app[API]._tasks))


def test_restart_refused_while_tour_active(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame(state_name="navigating")
            resp = await e.client.post("/api/stack/restart")
            assert resp.status == 409
            assert await resp.json() == {"error": "robot_moving"}
            assert e.docker.calls == []

    run(go())


def test_restart_allowed_without_frame_stale_link_or_idle(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            await e.login()
            for setup in (
                lambda: setattr(e.watcher, "frame", None),
                lambda: e.set_frame(state_name="navigating", mission_state_age_s=9.0),
                lambda: e.set_frame(state_name="unknown", mission_state_age_s=None),
                lambda: e.set_frame(state_name="idle"),
            ):
                e.docker.calls.clear()
                e.monitor._starting_since = None
                setup()
                resp = await e.client.post("/api/stack/restart")
                assert resp.status == 202
                await _settle(e)
                assert "restart" in e.docker.verbs()
            events = [r for r in e.journal() if r["event"] == "stack"]
            assert events and events[0]["operator"] == "pin"

    run(go())


def test_stack_start_and_log_and_disabled_control(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            await e.login()
            resp = await e.client.post("/api/stack/start")
            assert resp.status == 202
            await _settle(e)
            assert "exec:-d" in e.docker.verbs()
            log = await (await e.client.get("/api/stack/log")).json()
            assert log == {"lines": ["line1", "line2"]}
        (tmp_path / "off").mkdir()
        async with env(tmp_path / "off", up=False, container="") as e:
            await e.login()
            resp = await e.client.post("/api/stack/start")
            assert resp.status == 400
            assert await resp.json() == {"error": "stack_control_disabled"}

    run(go())


# -- журнал ------------------------------------------------------------------------


def test_journal_records_events_with_results(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login("00000000")
            await e.login()
            await e.client.post("/api/tour/current", json={"tour_id": "expo_two"})
            await e.client.post("/api/op/tour/stop")
            e.set_frame()
            await _public(e)
            await e.client.post("/api/auth/logout")
            events = e.journal()
            by = {}
            for r in events:
                by.setdefault(r["event"], []).append(r)
            assert [r["ok"] for r in by["auth_attempt"]] == [False, True]
            assert by["auth_attempt"][0]["reason"] == "wrong_pin"
            assert by["tour_current"][0]["tour_id"] == "expo_two"
            assert by["command"][0]["path"] == "/api/tour/stop"
            assert by["command"][0]["operator"] == "pin"
            assert by["command"][0]["status"] == 200
            assert by["public_start"][0]["ok"] is True
            assert by["logout"][0]["operator"] == "pin"
            assert all(isinstance(r["ts"], float) for r in events)

    run(go())


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
            assert resp.status == 409
            assert (await resp.json())["reason"] == "no_mission_fsm"

            e.set_frame(state_name="navigating")
            assert (await e.client.post("/api/stack/restart")).status == 409
            e.set_frame(state_name="navigating")
            e.clock.t += FRAME_MAX_AGE_S + 1
            e.docker.calls.clear()
            e.monitor._starting_since = None
            assert (await e.client.post("/api/stack/restart")).status == 202
            await _settle(e)

    run(go())
