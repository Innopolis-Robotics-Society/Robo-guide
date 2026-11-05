"""Host settings remain independent of ROS and never expose network credentials."""

import asyncio
from test_api import env, run


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
