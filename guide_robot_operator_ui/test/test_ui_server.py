"""Юниты lib/ui_server.py -- без rclpy, без ROS-графа.

Без pytest-asyncio (прецедент:
guide_robot_face/test/test_face_server.py) -- каждый тест сам гонит
event loop через asyncio.run().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from guide_robot_operator_ui.lib.ui_server import COMMAND_PATHS, UiServer

_MISSING_MEDIA_ROOT = Path(__file__).resolve().parent / "no-such-media-dir"
TOKEN = "bridge-token-for-tests"  # noqa: S105 -- тестовая фикстура, не секрет


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


async def _noop_command_logged(*, path: str, operator: str, status: int) -> None:
    del path, operator, status


def _new_server(
    *,
    media_root: Path | None = None,
    bridge_token: str = TOKEN,
    on_tours: Any = _ok_tours,
    on_tour_start: Any = _reject_tour_start,
    on_tour_stop: Any = _ok_no_message,
    on_go_home: Any = _ok_no_message,
    on_localization_reset: Any = _ok_no_message,
    on_costmaps_clear: Any = _ok_no_message,
    on_media: Any = _ok_empty_manifest,
    on_command_logged: Any = _noop_command_logged,
) -> UiServer:
    return UiServer(
        media_root=media_root or _MISSING_MEDIA_ROOT,
        bridge_token=bridge_token,
        on_tours=on_tours,
        on_tour_start=on_tour_start,
        on_tour_stop=on_tour_stop,
        on_go_home=on_go_home,
        on_localization_reset=on_localization_reset,
        on_costmaps_clear=on_costmaps_clear,
        on_media=on_media,
        on_command_logged=on_command_logged,
    )


def _token_header(token: str = TOKEN) -> dict[str, str]:
    return {"X-Bridge-Token": token}


# (path, json body) -- тело таково, что хэндлер доходит до коллбэка
_COMMANDS = [
    ("/api/tour/start", {"tour_id": "lab_demo"}),
    ("/api/tour/stop", None),
    ("/api/go_home", None),
    ("/api/localization/reset", {"confirm": True}),
    ("/api/costmaps/clear", None),
]


def test_empty_bridge_token_refuses_to_build_server() -> None:
    with pytest.raises(ValueError):
        _new_server(bridge_token="")


def test_media_route_registered_even_when_directory_missing() -> None:
    """Маршрут регистрируется безусловно, отсутствие каталога -- 404, не 500."""

    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/media/nope.jpg")
            assert resp.status == 404

    _run(body())


def test_media_files_force_revalidation(tmp_path: Path) -> None:
    """Файлы меняются под тем же URL -- киоск обязан сверяться с сервером."""
    (tmp_path / "a.jpg").write_bytes(b"x")

    async def body() -> None:
        async with TestClient(TestServer(_new_server(media_root=tmp_path).app)) as client:
            resp = await client.get("/media/a.jpg")
            assert resp.status == 200
            assert resp.headers.get("Cache-Control") == "no-cache"
            resp = await client.get("/api/tours")
            assert "Cache-Control" not in resp.headers

    _run(body())


def test_removed_routes_are_gone() -> None:
    """Страница, статика, промо и auth переехали в guide-launcher."""

    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            for method, path in (
                ("get", "/"),
                ("get", "/static/app.js"),
                ("get", "/api/promo"),
                ("get", "/promo/x.jpg"),
                ("post", "/api/auth/challenge"),
                ("get", "/api/auth/status"),
            ):
                resp = await getattr(client, method)(path)
                assert resp.status in (404, 405), (path, resp.status)

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


def test_get_routes_are_open_without_token() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            assert (await client.get("/api/tours")).status == 200
            assert (await client.get("/api/media/expo_city_model")).status == 200
            ws = await client.ws_connect("/ws")
            await ws.close()

    _run(body())


def test_api_tours_returns_callback_body() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/api/tours")
            assert resp.status == 200
            assert (await resp.json())["tours"][0]["id"] == "lab_demo"

    _run(body())


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


# -- гейт X-Bridge-Token --------------------------------------------------------


@pytest.mark.parametrize(("path", "payload"), _COMMANDS)
def test_command_without_token_is_403_and_callback_not_run(path: str, payload: Any) -> None:
    called = []

    async def spy(**kwargs: Any) -> tuple[int, dict]:
        called.append(kwargs)
        return 200, {"message": ""}

    async def body() -> None:
        server = _new_server(
            on_tour_start=spy,
            on_tour_stop=spy,
            on_go_home=spy,
            on_localization_reset=spy,
            on_costmaps_clear=spy,
        )
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(path, json=payload)
            assert resp.status == 403
            assert (await resp.json()) == {"error": "forbidden"}

    _run(body())
    assert called == []


@pytest.mark.parametrize(("path", "payload"), _COMMANDS)
def test_command_with_wrong_token_is_403(path: str, payload: Any) -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post(path, json=payload, headers=_token_header("wrong"))
            assert resp.status == 403
            resp = await client.post(path, json=payload, headers=_token_header(""))
            assert resp.status == 403
            resp = await client.post(path, json=payload, headers=_token_header(TOKEN + "x"))
            assert resp.status == 403

    _run(body())


@pytest.mark.parametrize(("path", "payload"), _COMMANDS)
def test_command_with_right_token_reaches_callback(path: str, payload: Any) -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post(path, json=payload, headers=_token_header())
            # tour/start в фикстуре отвечает 409 rejected -- это ответ коллбэка, не гейта
            assert resp.status == (409 if path == "/api/tour/start" else 200)

    _run(body())


def test_token_gate_runs_before_body_validation() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/tour/start", json={})
            assert resp.status == 403
            resp = await client.post("/api/tour/start", json={}, headers=_token_header())
            assert resp.status == 400

    _run(body())


def test_command_logged_callback_sees_operator_and_status() -> None:
    seen = []

    async def on_logged(*, path: str, operator: str, status: int) -> None:
        seen.append((path, operator, status))

    async def body() -> None:
        server = _new_server(on_command_logged=on_logged)
        async with TestClient(TestServer(server.app)) as client:
            await client.post(
                "/api/tour/stop", headers={**_token_header(), "X-Operator": "op_mook"}
            )
            await client.post("/api/go_home", headers={"X-Operator": "public"})

    _run(body())
    assert seen == [("/api/tour/stop", "op_mook", 200), ("/api/go_home", "public", 403)]


def test_tour_start_missing_tour_id_is_400_and_skips_callback() -> None:
    called = False

    async def on_start(*, tour_id: str) -> tuple[int, dict]:
        nonlocal called
        called = True
        del tour_id
        return 200, {"message": ""}

    async def body() -> None:
        async with TestClient(TestServer(_new_server(on_tour_start=on_start).app)) as client:
            resp = await client.post("/api/tour/start", json={}, headers=_token_header())
            assert resp.status == 400

    _run(body())
    assert called is False


def test_tour_start_forwards_tour_id_and_status() -> None:
    seen = {}

    async def on_start(*, tour_id: str) -> tuple[int, dict]:
        seen["tour_id"] = tour_id
        return 409, {"message": "rejected"}

    async def body() -> None:
        async with TestClient(TestServer(_new_server(on_tour_start=on_start).app)) as client:
            resp = await client.post(
                "/api/tour/start", json={"tour_id": "lab_demo"}, headers=_token_header()
            )
            assert resp.status == 409
            assert (await resp.json())["message"] == "rejected"

    _run(body())
    assert seen == {"tour_id": "lab_demo"}


def test_localization_reset_requires_explicit_confirm() -> None:
    called = False

    async def on_reset() -> tuple[int, dict]:
        nonlocal called
        called = True
        return 200, {"message": ""}

    async def body() -> None:
        server = _new_server(on_localization_reset=on_reset)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/localization/reset", json={}, headers=_token_header())
            assert resp.status == 400
            assert called is False
            resp2 = await client.post(
                "/api/localization/reset", json={"confirm": True}, headers=_token_header()
            )
            assert resp2.status == 200

    _run(body())
    assert called is True


def test_costmaps_clear_forwards_callback_status() -> None:
    async def unavailable() -> tuple[int, dict]:
        return 503, {"message": "service_unavailable"}

    async def body() -> None:
        server = _new_server(on_costmaps_clear=unavailable)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/costmaps/clear", headers=_token_header())
            assert resp.status == 503
            assert (await resp.json())["message"] == "service_unavailable"

    _run(body())


def test_command_paths_match_registered_post_routes() -> None:
    """Route-guard: новый POST под /api/ без записи в COMMAND_PATHS роняет тест."""
    server = _new_server()
    post_api_paths = {
        route.resource.canonical
        for route in server.app.router.routes()
        if route.method == "POST"
        and route.resource is not None
        and route.resource.canonical.startswith("/api/")
    }
    assert post_api_paths == COMMAND_PATHS
