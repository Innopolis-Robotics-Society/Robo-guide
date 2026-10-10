from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_launcher.bridge import Bridge, FrameWatcher
from guide_launcher.server import create_app
from guide_launcher.stack import StackMonitor
from helpers import Clock, FakeDocker, make_cfg

TOURS = {"tours": [{"id": "expo_one", "name": "Expo"}]}
FRAME = {"seq": 1, "state_name": "idle", "estop": False}


def make_backend(media_dir: Path) -> web.Application:
    calls: list[dict] = []

    async def tours(request):
        return web.json_response(TOURS)

    async def media_manifest(request):
        return web.json_response({"exhibit": request.match_info["eid"], "q": dict(request.query)})

    async def start(request):
        calls.append({"headers": dict(request.headers), "body": await request.json()})
        return web.json_response({"ok": True}, status=202)

    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.send_json(FRAME)
        async for msg in sock:
            if msg.type == web.WSMsgType.TEXT:
                if msg.data == "bye":
                    break
                await sock.send_str("echo:" + msg.data)
        await sock.close()
        return sock

    app = web.Application()
    app["calls"] = calls
    app.router.add_get("/api/tours", tours)
    app.router.add_get("/api/media/{eid}", media_manifest)
    app.router.add_post("/api/tour/start", start)
    app.router.add_get("/ws", ws)
    app.router.add_static("/media", media_dir)
    return app


@contextlib.asynccontextmanager
async def launcher(tmp_path: Path, **cfg_kw):
    media = tmp_path / "bridge_media"
    media.mkdir(exist_ok=True)
    backend = TestServer(make_backend(media))
    await backend.start_server()
    cfg = make_cfg(tmp_path, **cfg_kw)
    bridge = Bridge(str(backend.make_url("")), "tok")
    docker, clock = FakeDocker(), Clock()
    monitor = StackMonitor(cfg, bridge.probe, run=docker, clock=clock, sleep=clock.sleep)
    watcher = FrameWatcher(bridge, lambda: True, retry_s=0.05)
    app = create_app(cfg, monitor=monitor, bridge=bridge, watcher=watcher, run_background=False)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, backend, bridge, monitor, media
    finally:
        await client.close()
        await backend.close()


def run(coro):
    return asyncio.run(coro)


def test_index_static_and_config(tmp_path):
    async def go():
        async with launcher(tmp_path, always_promo=False, slide_interval_s=5.0) as (c, *_):
            r = await c.get("/")
            assert r.status == 200 and "launcher" in await r.text()
            assert (await (await c.get("/static/app.js")).text()) == "// js"
            body = await (await c.get("/api/config")).json()
            assert body == {
                "always_promo": False,
                "slide_interval_s": 5.0,
                "promo_interval_s": 10.0,
                "stack_control": True,
            }

    run(go())


def test_no_control_reported_when_container_empty(tmp_path):
    async def go():
        async with launcher(tmp_path, container="") as (c, *_):
            assert (await (await c.get("/api/config")).json())["stack_control"] is False

    run(go())


def test_everything_is_no_cache(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, bridge, monitor, media):
            (Path(tmp_path) / "promo" / "media").mkdir(parents=True)
            (Path(tmp_path) / "promo" / "media" / "a.jpg").write_bytes(b"jpg")
            (media / "x.bin").write_bytes(b"x")
            for path in (
                "/", "/static/app.js", "/api/config", "/api/promo", "/promo/a.jpg",
                "/api/stack/status", "/ros/api/tours", "/ros/media/x.bin", "/nope",
            ):  # fmt: skip
                r = await c.get(path)
                assert r.headers.get("Cache-Control") == "no-cache", path

    run(go())


def test_stack_status_endpoint(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, bridge, monitor, media):
            await monitor.poll()
            body = await (await c.get("/api/stack/status")).json()
            assert body["state"] == "UP"
            assert body["checks"] == {"container": True, "launch": True, "bridge": True}
            assert body["control"] is True

    run(go())


def test_promo_reloads_on_mtime_and_serves_files(tmp_path):
    promo = tmp_path / "promo"
    (promo / "media").mkdir(parents=True)
    (promo / "media" / "a.jpg").write_bytes(b"AAA")
    (promo / "media" / "b.jpg").write_bytes(b"BBB")
    yaml_path = promo / "promo.yaml"

    def item(name):
        return f"  - {{id: {name}, kind: image, file: {name}.jpg}}\n"

    async def go():
        async with launcher(tmp_path) as (c, *_):
            r = await c.get("/api/promo")
            assert await r.json() == {"rev": "none", "items": [], "promo_interval_s": 10.0}

            yaml_path.write_text("items:\n" + item("a"), encoding="utf-8")
            first = await (await c.get("/api/promo")).json()
            assert [i["path"] for i in first["items"]] == ["a.jpg"]

            yaml_path.write_text("items:\n" + item("a") + item("b"), encoding="utf-8")
            st = yaml_path.stat()
            os.utime(yaml_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            second = await (await c.get("/api/promo")).json()
            assert [i["id"] for i in second["items"]] == ["a", "b"]
            assert second["rev"] != first["rev"]

            yaml_path.unlink()
            assert (await (await c.get("/api/promo")).json())["items"] == []

            assert await (await c.get("/promo/b.jpg")).read() == b"BBB"

    run(go())


def test_promo_file_rejects_traversal_and_missing(tmp_path):
    promo = tmp_path / "promo"
    (promo / "media").mkdir(parents=True)
    (promo / "secret.txt").write_text("no", encoding="utf-8")

    async def go():
        async with launcher(tmp_path) as (c, *_):
            assert (await c.get("/promo/%2e%2e/secret.txt")).status == 404
            assert (await c.get("/promo/..%2fsecret.txt")).status == 404
            assert (await c.get("/promo/missing.jpg")).status == 404

    run(go())


def test_ros_proxy_allowlist_and_methods(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, *_):
            r = await c.get("/ros/api/tours")
            assert r.status == 200 and await r.json() == TOURS
            r = await c.get("/ros/api/media/e1?lang=ru")
            assert await r.json() == {"exhibit": "e1", "q": {"lang": "ru"}}

            for method in (c.post, c.put, c.delete):
                assert (await method("/ros/api/tours")).status == 405
            assert (await c.post("/ros/api/tour/start", json={"tour_id": "x"})).status == 405
            assert backend.app["calls"] == []

            for path in (
                "/ros/api/tour/start", "/ros/api/localization/reset", "/ros/api/media/",
                "/ros/media/", "/ros/", "/ros/api/media/../tour/start", "/ros/static/x",
            ):  # fmt: skip
                assert (await c.get(path)).status == 404, path

    run(go())


def test_ros_proxy_streams_files_with_ranges(tmp_path):
    payload = os.urandom(300_000)

    async def go():
        async with launcher(tmp_path) as (c, backend, bridge, monitor, media):
            (media / "big.bin").write_bytes(payload)
            r = await c.get("/ros/media/big.bin")
            assert r.status == 200
            assert r.headers["Content-Length"] == str(len(payload))
            assert await r.read() == payload

            r = await c.get("/ros/media/big.bin", headers={"Range": "bytes=10-19"})
            assert r.status == 206
            assert r.headers["Content-Range"] == f"bytes 10-19/{len(payload)}"
            assert await r.read() == payload[10:20]

            assert (await c.get("/ros/media/missing.bin")).status == 404

    run(go())


def test_ros_proxy_502_when_bridge_is_down(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, *_):
            await backend.close()
            assert (await c.get("/ros/api/tours")).status == 502
            assert (await c.get("/ros/ws")).status == 502

    run(go())


def test_ros_ws_proxy_both_directions(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, *_):
            ws = await c.ws_connect("/ros/ws")
            assert (await ws.receive_json(timeout=2)) == FRAME
            await ws.send_str("hello")
            assert (await ws.receive(timeout=2)).data == "echo:hello"
            await ws.send_str("bye")
            msg = await ws.receive(timeout=2)
            assert msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED)
            await ws.close()

    run(go())


def test_bridge_probe_and_command(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, bridge, *_):
            assert await bridge.probe() is True
            status, body = await bridge.command("/api/tour/start", {"tour_id": "t"}, "op_x")
            assert (status, body) == (202, {"ok": True})
            call = backend.app["calls"][0]
            assert call["headers"]["X-Bridge-Token"] == "tok"
            assert call["headers"]["X-Operator"] == "op_x"
            assert call["body"] == {"tour_id": "t"}

            status, body = await bridge.command("/api/nope", None, "x")
            assert status == 404 or status == 405

            await backend.close()
            assert await bridge.probe() is False
            assert await bridge.command("/api/tour/start", {}, "x") == (
                502,
                {"error": "bridge_unreachable"},
            )

    run(go())


def test_frame_watcher_tracks_frames_and_clears_when_not_up(tmp_path):
    async def go():
        async with launcher(tmp_path) as (c, backend, bridge, *_):
            up = [True]
            watcher = FrameWatcher(bridge, lambda: up[0], retry_s=0.05)
            task = asyncio.ensure_future(watcher.run_forever())
            try:
                for _ in range(60):
                    if watcher.latest():
                        break
                    await asyncio.sleep(0.05)
                frame = watcher.latest()
                assert frame is not None and frame.data == FRAME
                up[0] = False
                for _ in range(60):
                    if watcher.latest() is None:
                        break
                    await asyncio.sleep(0.05)
                assert watcher.latest() is None
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    run(go())


def test_shutdown_never_touches_the_stack(tmp_path):
    async def go():
        cfg = make_cfg(tmp_path, autostart_stack=False)
        docker, clock = FakeDocker(), Clock()

        async def probe():
            return True

        monitor = StackMonitor(cfg, probe, run=docker, clock=clock, sleep=clock.sleep)
        bridge = Bridge("http://127.0.0.1:9", "tok")
        app = create_app(cfg, monitor=monitor, bridge=bridge)
        client = TestClient(TestServer(app))
        await client.start_server()
        await asyncio.sleep(0.05)
        await client.close()
        verbs = docker.verbs()
        assert not {"start", "restart", "exec:-d", "exec:pkill"} & set(verbs)

    run(go())
