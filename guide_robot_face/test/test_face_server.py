"""Юнит-тесты face_server -- без rclpy, без ROS-графа.

Без pytest-asyncio (нет прецедента в репозитории и лишняя rosdep-зависимость
ради одного файла) -- каждый тест сам гонит свой event loop через
asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from guide_robot_face.face_server import FaceServer, apply_theme

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


def _themed(theme: str = "dark") -> dict:
    return {
        "global": {
            "theme": theme,
            "themes": {
                "dark": {"bg_color": "#05070a", "eye_color": "#eaf6ff", "halo_color": "#2a2d31"},
                "light": {"bg_color": "#ffffff", "eye_color": "#000000", "halo_color": "#d6d6d6"},
            },
        },
        "states": {},
    }


def test_apply_theme_overrides_yaml_theme() -> None:
    assert apply_theme(_themed("dark"), "light")["global"]["theme"] == "light"


def test_apply_theme_empty_keeps_yaml_theme() -> None:
    assert apply_theme(_themed("dark"), "")["global"]["theme"] == "dark"


def test_apply_theme_unknown_raises() -> None:
    with pytest.raises(ValueError, match="sepia"):
        apply_theme(_themed(), "sepia")


def test_apply_theme_without_themes_is_noop_for_empty() -> None:
    assert apply_theme(dict(STATES), "") == STATES


def test_apply_theme_manual_colors_override_theme() -> None:
    colors = {"eye_color": "#123", "halo_color": "", "bg_color": ""}
    g = apply_theme(_themed("dark"), "light", colors)["global"]
    assert g["themes"]["light"] == {
        "bg_color": "#ffffff",
        "eye_color": "#123",
        "halo_color": "#d6d6d6",
    }


def test_apply_theme_custom_theme_from_yaml() -> None:
    doc = _themed("night")
    doc["global"]["themes"]["night"] = {
        "bg_color": "#000814",
        "eye_color": "#7fd4ff",
        "halo_color": "#0b3a5a80",
    }
    assert apply_theme(doc, "")["global"]["theme"] == "night"


def test_apply_theme_missing_color_raises() -> None:
    doc = _themed("dark")
    del doc["global"]["themes"]["dark"]["halo_color"]
    with pytest.raises(ValueError, match="halo_color"):
        apply_theme(doc, "")


@pytest.mark.parametrize("bad", ["red", "#12", "ffffff", "#gggggg"])
def test_apply_theme_bad_color_raises(bad: str) -> None:
    with pytest.raises(ValueError, match="eye_color"):
        apply_theme(_themed(), "", {"eye_color": bad})


def test_apply_theme_unknown_color_key_raises() -> None:
    with pytest.raises(ValueError, match="lid_color"):
        apply_theme(_themed(), "", {"lid_color": "#000"})
