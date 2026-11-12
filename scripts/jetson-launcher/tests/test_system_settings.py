"""Host settings remain independent of ROS and never expose network credentials."""

import asyncio
from pathlib import Path

from guide_launcher.system_settings import SystemSettings
from test_api import env, run


def test_face_cursor_auth_persistence_and_validation(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            path = "/api/v1/display/face-cursor"
            assert (await e.client.get(path)).status == 401
            assert (await e.client.post(path, json={"visible": True})).status == 401
            await e.login()
            assert await (await e.client.get(path)).json() == {"visible": False}
            for invalid in ({}, {"visible": "false"}, {"visible": 1}, {"visible": None},
                            {"visible": True, "path": "/tmp/other"}):
                assert (await e.client.post(path, json=invalid)).status == 400
            for visible in (True, False):
                response = await e.client.post(path, json={"visible": visible})
                assert response.status == 200
                assert await response.json() == {"visible": visible}
                # A fresh instance reads the persisted setting, without ROS or cached state.
                fresh = SystemSettings(e.app["api"])
                request = type("Request", (), {"method": "GET"})()
                assert fresh is not e.app["system_settings"]
                import json
                assert json.loads((await fresh.face_cursor(request)).text) == {"visible": visible}
            assert not e.calls

    run(go())


def test_face_cursor_failed_save_keeps_previous_setting(tmp_path, monkeypatch):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            path = "/api/v1/display/face-cursor"
            assert (await e.client.post(path, json={"visible": True})).status == 200
            state_dir = Path(e.app["api"].cfg.state_dir)
            before = set(state_dir.iterdir())

            def fail(*args):
                raise OSError("read-only filesystem")

            monkeypatch.setattr("guide_launcher.system_settings.os.replace", fail)
            assert (await e.client.post(path, json={"visible": False})).status == 503
            assert await (await e.client.get(path)).json() == {"visible": True}
            assert set(state_dir.iterdir()) == before

    run(go())


def test_settings_auth_and_disabled_helpers(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            assert (await e.client.get("/api/v1/wifi")).status == 401
            assert (await e.client.post("/api/v1/audio/output", json={"volume": 50})).status == 401
            await e.login()
            assert (await e.client.get("/api/v1/audio/output")).status == 503
            assert (
                await e.client.post("/api/v1/audio/output", json={"volume": 101})
            ).status == 400

    run(go())


def test_pose_validation_and_auth(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            body = {"map_version": "v", "pose": {"x": 1, "y": 2, "yaw": 0}}
            assert (await e.client.post("/api/v1/missions/go-to-pose", json=body)).status == 401
            await e.login()
            assert (
                await e.client.post(
                    "/api/v1/missions/go-to-pose", json={**body, "pose": {"x": True}}
                )
            ).status == 400
            assert (
                await e.client.post(
                    "/api/v1/localization/reset",
                    json={**body, "preset_id": "home", "confirm": True},
                )
            ).status == 400

    run(go())


def test_wifi_operation_hides_credentials_and_survives_ros_disconnect(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()

            async def idle(*args):
                return 200, {"success": True}

            e.app["api"].bridge.command = idle
            settings = e.app["system_settings"]
            settings.api.cfg.wifi_socket = "/test-helper"
            release = asyncio.Event()

            async def call(*args):
                await release.wait()
                return 200, {"connected": True}

            settings.call = call
            body = {"ssid": "example", "password": "very-secret-password"}
            headers = {"Idempotency-Key": "wifi-one"}
            first = await (
                await e.client.post("/api/v1/wifi/connect", json=body, headers=headers)
            ).json()
            repeat = await (
                await e.client.post("/api/v1/wifi/connect", json=body, headers=headers)
            ).json()
            assert first["id"] == repeat["id"]
            e.watcher.frame = None
            snapshot = await (await e.client.get("/api/v1/state")).json()
            assert "very-secret-password" not in str(snapshot)
            assert snapshot["operations"][0]["status"] in ("pending", "running")
            release.set()
            await asyncio.sleep(0.01)
            operation = await (await e.client.get("/api/v1/operations/" + first["id"])).json()
            assert operation["status"] == "succeeded"

    run(go())
