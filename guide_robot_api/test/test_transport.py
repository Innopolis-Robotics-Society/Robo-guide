"""Private transport security, reconnect snapshots, media caching and cleanup."""

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from guide_robot_api.api_node import RobotApiNode
from guide_robot_api.lib.http_server import ApiServer


def test_private_token_health_range_and_snapshot(tmp_path):
    async def go():
        (tmp_path / "movie.mp4").write_bytes(b"0123456789")
        calls = []

        async def dispatch(method, resource, body):
            calls.append(resource)
            if resource == "timeout":
                raise asyncio.TimeoutError()
            if resource == "after-dispatch":
                raise TypeError("unexpected adapter failure")
            return 200, {"ok": True}

        server = ApiServer(media_root=tmp_path, bridge_token="secret", dispatch=dispatch)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            assert (await client.get("/healthz")).status == 200 and not calls
            for path in ("/internal/tours", "/internal/ws", "/media/movie.mp4"):
                assert (await client.get(path)).status == 403
            headers = {"X-Bridge-Token": "secret"}
            assert (await client.post("/internal/timeout", json={}, headers=headers)).status == 504
            assert (
                await client.post("/internal/after-dispatch", json={}, headers=headers)
            ).status == 500
            assert (await client.post("/internal/invalid", json=[], headers=headers)).status == 400
            response = await client.get(
                "/media/movie.mp4", headers={**headers, "Range": "bytes=2-5"}
            )
            assert response.status == 206 and await response.read() == b"2345"
            await server.push({"seq": 1})
            for _ in range(2):
                ws = await client.ws_connect("/internal/ws", headers=headers)
                assert await ws.receive_json(timeout=1) == {"seq": 1}
                await ws.close()
        finally:
            await client.close()

    asyncio.run(go())


def test_broadcast_client_set_may_change_while_awaiting():
    async def go():
        server = ApiServer(media_root="/missing", bridge_token="secret", dispatch=None)

        class Client:
            async def send_json(self, frame):
                server.clients.discard(self)

        server.clients.update([Client(), Client()])
        await server.push({"state": "idle"})
        assert not server.clients

    asyncio.run(go())


def test_media_errors_not_cached_success_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("guide_robot_api.api_node.time.monotonic", lambda: clock[0])
    node = RobotApiNode.__new__(RobotApiNode)
    node._content_language = "ru"
    node._service_timeout_s = 1
    node._media_manifest_cache = {}
    node._get_exhibit_content_client = object()
    node._get_exhibit_media_client = object()
    content = SimpleNamespace(title="Exhibit", chunks=[SimpleNamespace(chunk_id="intro")])
    media = SimpleNamespace(ok=False, items=[])
    calls = []

    async def call(make, timeout, *, name):
        calls.append(name)
        return content if name == "get_exhibit_content" else media

    node._call_ros = call

    async def go():
        assert (await node._on_api_media(exhibit_id="e"))[0] == 503
        assert not node._media_manifest_cache
        media.ok = True
        assert (await node._on_api_media(exhibit_id="e"))[0] == 200
        count = len(calls)
        await node._on_api_media(exhibit_id="e")
        assert len(calls) == count
        clock[0] += 31
        await node._on_api_media(exhibit_id="e")
        assert len(calls) == count + 2
        content.title = ""
        clock[0] += 31
        assert (await node._on_api_media(exhibit_id="e"))[0] == 503

    asyncio.run(go())
