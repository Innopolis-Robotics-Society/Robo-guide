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
from guide_robot_operator_ui.lib.ui_server import GATED_COMMAND_PATHS, UiServer

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
_MISSING_MEDIA_ROOT = WEB_ROOT.parent / "no-such-media-dir"
_MISSING_PROMO_ROOT = WEB_ROOT.parent / "no-such-promo-dir"
VALID_TOKEN = "valid-token-for-tests"  # noqa: S105 -- тестовая фикстура, не секрет


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


async def _ok_empty_promo() -> tuple[int, dict]:
    return 200, {"items": [], "promo_interval_s": 10.0}


async def _ok_auth_challenge() -> tuple[int, dict]:
    return 200, {"nonce": "test-nonce", "backends": ["pin"]}


async def _ok_auth_verify(**kwargs: Any) -> tuple[int, dict]:
    del kwargs
    return 200, {"token": VALID_TOKEN, "expires_at": 0.0, "operator": "pin"}


async def _ok_auth_logout() -> tuple[int, dict]:
    return 200, {"ok": True}


async def _ok_auth_status() -> tuple[int, dict]:
    return 200, {"active": False, "expires_at": None, "operator": None}


async def _allow_all_auth_check(token: str | None) -> tuple[bool, str]:
    """Дефолт `_new_server()`: гейт пропускает всё -- тесты команд не про auth."""
    del token
    return True, "pin"


async def _auth_check_valid_token(token: str | None) -> tuple[bool, str]:
    """Для тестов гейта: валиден ровно VALID_TOKEN, всё остальное -- нет."""
    return (True, "pin") if token == VALID_TOKEN else (False, "")


async def _noop_auth_touch(token: str) -> None:
    del token


async def _noop_command_logged(*, path: str, operator: str, status: int) -> None:
    del path, operator, status


def _new_server(
    *,
    media_root: Path | None = None,
    promo_root: Path | None = None,
    on_tours: Any = _ok_tours,
    on_tour_start: Any = _reject_tour_start,
    on_tour_stop: Any = _ok_no_message,
    on_go_home: Any = _ok_no_message,
    on_localization_reset: Any = _ok_no_message,
    on_media: Any = _ok_empty_manifest,
    on_promo: Any = _ok_empty_promo,
    on_auth_challenge: Any = _ok_auth_challenge,
    on_auth_verify: Any = _ok_auth_verify,
    on_auth_logout: Any = _ok_auth_logout,
    on_auth_status: Any = _ok_auth_status,
    on_auth_check: Any = _allow_all_auth_check,
    on_auth_touch: Any = _noop_auth_touch,
    on_command_logged: Any = _noop_command_logged,
) -> UiServer:
    return UiServer(
        web_root=WEB_ROOT,
        media_root=media_root or _MISSING_MEDIA_ROOT,
        promo_root=promo_root or _MISSING_PROMO_ROOT,
        on_tours=on_tours,
        on_tour_start=on_tour_start,
        on_tour_stop=on_tour_stop,
        on_go_home=on_go_home,
        on_localization_reset=on_localization_reset,
        on_media=on_media,
        on_promo=on_promo,
        on_auth_challenge=on_auth_challenge,
        on_auth_verify=on_auth_verify,
        on_auth_logout=on_auth_logout,
        on_auth_status=on_auth_status,
        on_auth_check=on_auth_check,
        on_auth_touch=on_auth_touch,
        on_command_logged=on_command_logged,
    )


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


def test_promo_route_registered_even_when_directory_missing() -> None:
    """design F2 -- тот же паттерн C7, что и /media/*: 404, не 500."""

    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/promo/nope.jpg")
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


def test_api_promo_returns_callback_body_without_token() -> None:
    """design F2, критерий 10: без токена, тот же паттерн, что /api/tours."""

    async def on_promo() -> tuple[int, dict]:
        return 200, {
            "items": [{"id": "p1", "kind": "image", "path": "hall.jpg"}],
            "promo_interval_s": 6.0,
        }

    async def body() -> None:
        async with TestClient(TestServer(_new_server(on_promo=on_promo).app)) as client:
            resp = await client.get("/api/promo")
            assert resp.status == 200
            data = await resp.json()
            assert data["items"][0]["id"] == "p1"
            assert data["promo_interval_s"] == 6.0

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
            resp = await client.post("/api/tour/start", json={}, headers=_auth_header(VALID_TOKEN))
            assert resp.status == 400

    _run(body())
    assert called is False


def test_tour_start_forwards_tour_id_and_status() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post(
                "/api/tour/start", json={"tour_id": "lab_demo"}, headers=_auth_header(VALID_TOKEN)
            )
            assert resp.status == 409
            assert (await resp.json())["message"] == "rejected"

    _run(body())


def test_tour_stop_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/tour/stop", headers=_auth_header(VALID_TOKEN))
            assert resp.status == 200

    _run(body())


def test_go_home_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/go_home", headers=_auth_header(VALID_TOKEN))
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
            resp = await client.post(
                "/api/localization/reset", json={}, headers=_auth_header(VALID_TOKEN)
            )
            assert resp.status == 400
            resp2 = await client.post(
                "/api/localization/reset",
                json={"confirm": True},
                headers=_auth_header(VALID_TOKEN),
            )
            assert resp2.status == 200

    _run(body())
    assert called is True


# -- гейт аутентификации (design E2) ------------------------------------------


def test_gated_command_without_token_is_401() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/tour/stop")
            assert resp.status == 401
            assert (await resp.json())["error"] == "auth_required"

    _run(body())


def test_gated_command_with_invalid_token_is_401_and_command_not_run() -> None:
    called = False

    async def on_stop() -> tuple[int, dict]:
        nonlocal called
        called = True
        return 200, {"message": ""}

    async def body() -> None:
        server = _new_server(on_tour_stop=on_stop, on_auth_check=_auth_check_valid_token)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/tour/stop", headers=_auth_header("garbage"))
            assert resp.status == 401

    _run(body())
    assert called is False


def test_gated_command_with_valid_token_touches_session_on_success() -> None:
    touched = []

    async def on_touch(token: str) -> None:
        touched.append(token)

    async def body() -> None:
        server = _new_server(on_auth_check=_auth_check_valid_token, on_auth_touch=on_touch)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post("/api/tour/stop", headers=_auth_header(VALID_TOKEN))
            assert resp.status == 200

    _run(body())
    assert touched == [VALID_TOKEN]


def test_gated_command_rejected_by_state_does_not_touch_session() -> None:
    """409 (отказ по состоянию робота) -- не "успешная команда", окно не продлевается."""
    touched = []

    async def on_touch(token: str) -> None:
        touched.append(token)

    async def body() -> None:
        server = _new_server(on_auth_check=_auth_check_valid_token, on_auth_touch=on_touch)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/api/tour/start",
                json={"tour_id": "lab_demo"},
                headers=_auth_header(VALID_TOKEN),
            )
            assert resp.status == 409

    _run(body())
    assert touched == []


def test_ungated_command_logged_callback_sees_operator_and_status() -> None:
    seen = []

    async def on_logged(*, path: str, operator: str, status: int) -> None:
        seen.append((path, operator, status))

    async def body() -> None:
        server = _new_server(on_auth_check=_auth_check_valid_token, on_command_logged=on_logged)
        async with TestClient(TestServer(server.app)) as client:
            await client.post("/api/tour/stop", headers=_auth_header(VALID_TOKEN))

    _run(body())
    assert seen == [("/api/tour/stop", "pin", 200)]


def test_auth_paths_are_not_gated() -> None:
    """/api/auth/* не требует токена -- иначе вход стал бы невозможен."""

    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            for resp in (
                await client.post("/api/auth/challenge"),
                await client.post("/api/auth/verify", json={"nonce": "n", "backend": "pin"}),
                await client.post("/api/auth/logout"),
                await client.get("/api/auth/status"),
            ):
                assert resp.status != 401

    _run(body())


def test_auth_challenge_returns_callback_body() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/auth/challenge")
            assert resp.status == 200
            assert (await resp.json())["nonce"] == "test-nonce"

    _run(body())


def test_auth_verify_forwards_body_as_kwargs() -> None:
    seen = {}

    async def on_verify(**kwargs: Any) -> tuple[int, dict]:
        seen.update(kwargs)
        return 200, {"token": VALID_TOKEN, "expires_at": 0.0, "operator": "pin"}

    async def body() -> None:
        server = _new_server(on_auth_verify=on_verify)
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.post(
                "/api/auth/verify", json={"nonce": "abc", "backend": "pin", "pin": "changeme"}
            )
            assert resp.status == 200

    _run(body())
    assert seen == {"nonce": "abc", "backend": "pin", "pin": "changeme"}


def test_auth_verify_invalid_json_is_400() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post(
                "/api/auth/verify", data="not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    _run(body())


def test_auth_logout_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.post("/api/auth/logout")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    _run(body())


def test_auth_status_calls_callback() -> None:
    async def body() -> None:
        async with TestClient(TestServer(_new_server().app)) as client:
            resp = await client.get("/api/auth/status")
            assert resp.status == 200
            assert (await resp.json())["active"] is False

    _run(body())


def test_gated_command_paths_match_registered_command_routes() -> None:
    """Тест-«сторож» (design E2, критерий 18): новый командный роут без

    добавления в GATED_COMMAND_PATHS роняет этот тест, а не остаётся
    незащищённым по забывчивости.
    """
    server = _new_server()
    post_api_paths = {
        route.resource.canonical
        for route in server.app.router.routes()
        if route.method == "POST"
        and route.resource is not None
        and route.resource.canonical.startswith("/api/")
        and not route.resource.canonical.startswith("/api/auth/")
    }
    assert post_api_paths == GATED_COMMAND_PATHS


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
