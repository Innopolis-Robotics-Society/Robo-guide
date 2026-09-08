"""Юниты lib/ui_server.py -- без rclpy, без ROS-графа (design C2).

Без pytest-asyncio (прецедент:
guide_robot_face/test/test_face_server.py) -- каждый тест сам гонит
event loop через asyncio.run().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer
from guide_robot_operator_ui.lib.ui_server import UiServer

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
_MISSING_MEDIA_ROOT = WEB_ROOT.parent / "no-such-media-dir"


def _run(coro: Any) -> None:
    asyncio.run(coro)


async def _ok_tours() -> tuple[int, dict]:
    return 200, {"tours": [{"id": "lab_demo", "name": "Демо"}]}


async def _reject_tour_start(*, tour_id: str) -> tuple[int, dict]:
    del tour_id
    return 409, {"message": "rejected"}


async def _ok_no_message() -> tuple[int, dict]:
    return 200, {"message": ""}


async def _ok_empty_manifest(*, exhibit_id: str) -> tuple[int, dict]:
    del exhibit_id
    return 200, {"title": "", "chunk_ids": [], "items": []}


def _new_server(
    *,
    media_root: Path | None = None,
    on_tours: Any = _ok_tours,
    on_tour_start: Any = _reject_tour_start,
    on_tour_stop: Any = _ok_no_message,
    on_go_home: Any = _ok_no_message,
    on_localization_reset: Any = _ok_no_message,
    on_media: Any = _ok_empty_manifest,
) -> UiServer:
    return UiServer(
        web_root=WEB_ROOT,
        media_root=media_root or _MISSING_MEDIA_ROOT,
        on_tours=on_tours,
        on_tour_start=on_tour_start,
        on_tour_stop=on_tour_stop,
        on_go_home=on_go_home,
        on_localization_reset=on_localization_reset,
        on_media=on_media,
    )


def test_index_serves_web_root_index_html() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            assert "guide_robot_operator_ui" in await resp.text()

    _run(body())


def test_static_asset_served() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/static/app.js")
            assert resp.status == 200

    _run(body())


def test_media_route_registered_even_when_directory_missing() -> None:
    """design C7: маршрут регистрируется безусловно, отсутствие каталога -- не 500."""

    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/media/nope.jpg")
            assert resp.status == 404

    _run(body())


def test_ws_receives_last_frame_on_connect() -> None:
    async def body() -> None:
        server = _new_server()
        await server.push({"seq": 1, "state_name": "idle"})
        async with TestClient(TestServer(server.app)) as client:
            ws = await client.ws_connect("/ws")
            msg = await ws.receive_json()
            assert msg == {"seq": 1, "state_name": "idle"}
            await ws.close()

    _run(body())


def test_ws_connect_with_no_prior_frame_gets_nothing_immediately() -> None:
    async def body() -> None:
        server = _new_server()
        async with TestClient(TestServer(server.app)) as client:
            ws = await client.ws_connect("/ws")
            await server.push({"seq": 1, "state_name": "narrating"})
            msg = await ws.receive_json()
            assert msg == {"seq": 1, "state_name": "narrating"}
            await ws.close()

    _run(body())


def test_push_broadcasts_to_multiple_clients() -> None:
    async def body() -> None:
        server = _new_server()
        async with TestClient(TestServer(server.app)) as client:
            ws_a = await client.ws_connect("/ws")
            ws_b = await client.ws_connect("/ws")
            frame = {"seq": 7, "state_name": "held"}
            await server.push(frame)
            assert await ws_a.receive_json() == frame
            assert await ws_b.receive_json() == frame
            await ws_a.close()
            await ws_b.close()

    _run(body())


def test_api_tours_returns_callback_body() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/api/tours")
            assert resp.status == 200
            assert (await resp.json())["tours"][0]["id"] == "lab_demo"

    _run(body())


def test_tour_start_missing_tour_id_is_400_and_skips_callback() -> None:
    called = False

    async def on_start(*, tour_id: str) -> tuple[int, dict]:
        nonlocal called
        called = True
        del tour_id
        return 200, {"message": ""}

    async def body() -> None:
        async with TestClient(TestServer(_new_server(on_tour_start=on_start).app)) as client:
            resp = await client.post("/api/tour/start", json={})
            assert resp.status == 400

    _run(body())
    assert called is False


def test_tour_start_forwards_tour_id_and_status() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/tour/start", json={"tour_id": "lab_demo"})
            assert resp.status == 409
            assert (await resp.json())["message"] == "rejected"

    _run(body())


def test_tour_stop_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/tour/stop")
            assert resp.status == 200

    _run(body())


def test_go_home_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/go_home")
            assert resp.status == 200

    _run(body())


def test_localization_reset_requires_explicit_confirm() -> None:
    called = False

    async def on_reset() -> tuple[int, dict]:
        nonlocal called
        called = True
        return 200, {"message": ""}

    async def body() -> None:
        server = _new_server(on_localization_reset=on_reset)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/localization/reset", json={})
            assert resp.status == 400
            resp2 = await client.post("/api/localization/reset", json={"confirm": True})
            assert resp2.status == 200

    _run(body())
    assert called is True


def test_media_forwards_exhibit_id_from_path() -> None:
    seen = {}

    async def on_media(*, exhibit_id: str) -> tuple[int, dict]:
        seen["exhibit_id"] = exhibit_id
        return 200, {"title": "Макет", "chunk_ids": ["c1"], "items": []}

    async def body() -> None:
        async with TestClient(TestServer(_new_server(on_media=on_media).app)) as client:
            resp = await client.get("/api/media/expo_city_model")
            assert resp.status == 200
            assert (await resp.json())["title"] == "Макет"

    _run(body())
    assert seen["exhibit_id"] == "expo_city_model"
