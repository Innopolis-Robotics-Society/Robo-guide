"""System helpers tested without touching host audio or networking."""

import asyncio
import importlib.util
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

HELPERS = Path(__file__).parents[1] / "helpers"


def load(name):
    spec = importlib.util.spec_from_file_location(name, HELPERS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_limit_and_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv("GUIDE_AUDIO_SINK", "test-speaker")
    monkeypatch.setenv("GUIDE_AUDIO_STATE", str(tmp_path / "volume.json"))
    audio = load("audio_service").Audio()
    calls = []

    async def pactl(*args):
        calls.append(args)
        if args[0] == "--format=json":
            return json.dumps(
                [
                    {
                        "name": "test-speaker",
                        "mute": False,
                        "volume": {"left": {"value_percent": "10%"}},
                    }
                ]
            ).encode()
        return b""

    audio.pactl = pactl

    async def run():
        app = web.Application()
        app.router.add_post("/output", audio.handle)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/output", json={"volume": 50, "muted": False})
            assert response.status == 200
            assert (await response.json())["volume"] == 50
            assert ("set-sink-volume", "test-speaker", "6553") in calls
            assert json.loads((tmp_path / "volume.json").read_text())["volume"] == 50
            assert (await client.post("/output", json={"volume": 101})).status == 400

    asyncio.run(run())


def test_wifi_excludes_service_interface(monkeypatch):
    import pytest

    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "service-test")
    monkeypatch.setenv("GUIDE_WIFI_HOTSPOT_INTERFACE", "service-test")
    with pytest.raises(ValueError, match="excluded"):
        load("wifi_service").Wifi()


def test_wifi_failure_restores_previous_profile_without_leaking_secret(monkeypatch):
    import pytest

    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    calls = []

    class Manager:
        async def call_add_and_activate_connection(self, settings, path, specific):
            calls.append("connect")
            return "/profile/new", "/active/new"

        async def call_activate_connection(self, profile, path, specific):
            calls.append(profile)
            return "/active/old"

    async def device():
        return Manager(), "/device", {"ActiveConnection": "/active/old"}

    async def props(*args):
        return {"Connection": "/profile/old"}

    async def profiles():
        return []

    async def state():
        return {"networks": []}

    async def wait(path, active):
        return active == "/active/old"

    wifi.device, wifi.props, wifi.profiles, wifi.state, wifi.wait_active = (
        device,
        props,
        profiles,
        state,
        wait,
    )

    async def run():
        app = web.Application()
        app.router.add_post("/{action}", wifi.handle)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/connect", json={"ssid": "test", "password": "password-123"}
            )
            assert response.status == 409
            data = await response.json()
            assert data["restored_previous"] is True
            assert "password-123" not in str(data)
            assert calls == ["connect", "/profile/old"]

    asyncio.run(run())


def test_wifi_reports_address_stage_without_requiring_internet(monkeypatch):
    import pytest

    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    observations = iter([
        {"State": 70, "ActiveConnection": "/active/new"},
        {"State": 100, "ActiveConnection": "/active/new"},
    ])
    phases = []

    async def props(*args):
        return next(observations)

    async def sleep(delay):
        phases.append(wifi.phase)

    wifi.props = props
    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert asyncio.run(wifi.wait_active("/device", "/active/new"))
    assert phases == ["obtaining_address"]


def test_wifi_scan_waits_for_completion_and_preserves_connection(monkeypatch):
    import pytest

    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    module = load("wifi_service")
    wifi = module.Wifi()
    calls = []
    timestamps = iter([100, 100, 100, 100, 101])

    class Wireless:
        async def call_request_scan(self, options):
            calls.append("scan")

    async def device():
        return None, "/device", {}

    async def proxy(*args):
        return Wireless()

    async def props(*args):
        stamp = next(timestamps)
        calls.append(stamp)
        return {"LastScan": stamp}

    async def state():
        calls.append("state")
        return {"networks": [{"ssid": "newly-discovered"}]}

    async def sleep(delay):
        pass

    wifi.device, wifi.proxy, wifi.props, wifi.state = device, proxy, props, state
    monkeypatch.setattr(module.asyncio, "sleep", sleep)

    async def run():
        app = web.Application()
        app.router.add_post("/{action}", wifi.handle)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/scan", json={})
            assert response.status == 200
            assert (await response.json())["networks"][0]["ssid"] == "newly-discovered"
            assert calls == [100, "scan", 100, 100, 100, 101, "state"]

    asyncio.run(run())
