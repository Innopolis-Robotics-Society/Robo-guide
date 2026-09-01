"""Юнит-тесты face_server -- без rclpy, без ROS-графа.

Без pytest-asyncio (нет прецедента в репозитории и лишняя rosdep-зависимость
ради одного файла) -- каждый тест сам гонит свой event loop через
asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from guide_robot_face.face_server import FaceServer

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
STATES = {
    "global": {"eye_color": "#eaf6ff", "bg_color": "#05070a"},
    "states": {"idle": {"w": 1, "h": 1, "rr": 1, "rot": 0, "curve": 0.0, "gx": 0, "gy": 0}},
}


def _run(coro):
    return asyncio.run(coro)


def _new_server() -> FaceServer:
    return FaceServer(web_root=WEB_ROOT, states=STATES)


def test_states_json_matches_input() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/states.json")
            assert resp.status == 200
            assert await resp.json() == STATES

    _run(body())


def test_index_serves_web_root_index_html() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            assert "guide_robot_face" in await resp.text()

    _run(body())


def test_static_asset_served() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/face.js")
            assert resp.status == 200

    _run(body())


def test_ws_receives_last_frame_on_connect() -> None:
    async def body() -> None:
        server = _new_server()
        await server.push("thinking", 12.5, 3)
        async with TestClient(TestServer(server.app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await ws.receive_json()
            assert msg == {"state": "thinking", "gaze_az": 12.5, "seq": 3}
            await ws.close()

    _run(body())


def test_ws_connect_with_no_prior_frame_gets_nothing_immediately() -> None:
    async def body() -> None:
        server = _new_server()
        async with TestClient(TestServer(server.app)) as client:
            ws = await client.ws_connect("/ws")
            await server.push("speaking", 0.0, 1)
            msg = await ws.receive_json()
            assert msg == {"state": "speaking", "gaze_az": 0.0, "seq": 1}
            await ws.close()

    _run(body())


def test_push_broadcasts_to_multiple_clients() -> None:
    async def body() -> None:
        server = _new_server()
        async with TestClient(TestServer(server.app)) as client:
            ws_a = await client.ws_connect("/ws")
            ws_b = await client.ws_connect("/ws")
            await server.push("error", -5.0, 7)
            frame = {"state": "error", "gaze_az": -5.0, "seq": 7}
            assert json.loads(await ws_a.receive_str()) == frame
            assert json.loads(await ws_b.receive_str()) == frame
            await ws_a.close()
            await ws_b.close()

    _run(body())
