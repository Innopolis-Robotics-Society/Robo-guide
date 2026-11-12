"""System helpers tested without touching host audio or networking."""

import asyncio
import importlib.util
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web
import pytest

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


@pytest.mark.parametrize("eligible", [True, False])
@pytest.mark.parametrize("transport_error", [True, False])
def test_wifi_failure_restores_previous_profile_without_leaking_secret(monkeypatch, eligible, transport_error):
    import pytest

    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    calls = []

    class Manager:
        async def call_add_and_activate_connection(self, settings, path, specific):
            calls.append("connect")
            if transport_error:
                raise RuntimeError("password-123")
            return "/profile/new", "/active/new"

        async def call_activate_connection(self, profile, path, specific):
            calls.append(profile)
            return "/active/old"

    async def device():
        return Manager(), "/device", {"ActiveConnection": "/active/old"}

    async def props(*args):
        return {"Connection": "/profile/old"}

    async def profiles():
        # profiles() only admits infrastructure profiles on the managed adapter.
        return [{"id": "old", "path": "/profile/old", "ssid": "old-network"}] if eligible else []

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
            assert response.status == (503 if transport_error else 409)
            data = await response.json()
            assert data["restored_previous"] is eligible
            assert "password-123" not in str(data)
            assert calls == (["connect", "/profile/old"] if eligible else ["connect"])

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


@pytest.mark.parametrize("connected,has_ip", [(True, True), (True, False), (False, True)])
def test_wifi_status_returns_one_saved_connection_per_ssid_without_secrets(monkeypatch, connected, has_ip):
    dbus = pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    module = load("wifi_service")
    wifi = module.Wifi()
    variant = dbus.Variant
    settings = {}
    for ident, name, timestamp in [("old", "guide-phone", 0), ("active", "phone", 123)]:
        settings["/profile/" + ident] = {
            "connection": {
                "uuid": variant("s", ident), "id": variant("s", name),
                "type": variant("s", "802-11-wireless"),
                "interface-name": variant("s", "test-radio"),
                "timestamp": variant("t", timestamp),
            },
            "802-11-wireless": {"ssid": variant("ay", b"phone")},
            "802-11-wireless-security": {
                "key-mgmt": variant("s", "wpa-psk"),
                "psk": variant("s", "must-not-leak"),
            },
        }

    class Proxy:
        def __init__(self, path):
            self.path = path

        async def call_list_connections(self):
            return list(settings)

        async def call_get_settings(self):
            return settings[self.path]

        async def call_get_all_access_points(self):
            return ["/ap"]

    async def proxy(path, _interface):
        return Proxy(path)

    async def device():
        return None, "/device", {
            "State": 100 if connected else 30,
            "ActiveConnection": "/active" if connected else "/",
            "Ip4Config": "/wifi-ip4" if has_ip else "/",
        }

    async def props(path, _interface):
        return {
            "/device": {"ActiveAccessPoint": "/ap" if connected else "/"},
            "/ap": {"Ssid": b"phone", "Strength": 80, "RsnFlags": 0x100, "Flags": 1},
            "/active": {"Uuid": "active"},
            "/wifi-ip4": {"AddressData": [{"address": variant("s", "172.20.10.5"),
                                            "prefix": variant("u", 28)}]},
        }[path]

    wifi.proxy, wifi.device, wifi.props = proxy, device, props
    state = asyncio.run(wifi.state())
    assert state["current_profile_id"] == ("active" if connected else None)
    assert state["ipv4_addresses"] == (["172.20.10.5"] if connected and has_ip else [])
    assert state["profiles"] == [
        {"id": "active", "name": "phone", "last_connected": 123, "ssid": "phone"},
    ]
    assert "must-not-leak" not in json.dumps(state)
    assert "/profile/" not in json.dumps(state)


def test_wifi_saved_profile_activates_exact_selection_without_creating_duplicate(monkeypatch):
    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    calls = []

    class Manager:
        async def call_activate_connection(self, profile, path, specific):
            calls.append(profile)
            return "/active/selected"

    async def device():
        return Manager(), "/device", {"ActiveConnection": "/"}

    async def profiles():
        return [
            {"id": ident, "ssid": "phone", "path": "/profile/" + ident,
             "last_connected": 100 if ident == "working" else 0}
            for ident in ["failed", "working"]
        ]

    async def wait(path, active):
        return active == "/active/selected"

    wifi.device, wifi.profiles, wifi.wait_active = device, profiles, wait

    async def run():
        app = web.Application()
        app.router.add_post("/{action}", wifi.handle)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/connect", json={"profile_id": "working"})
            assert response.status == 200
            assert (await response.json())["connected"] is True
        assert calls == ["/profile/working"]

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "failed", "activate_error", "update_error", "saved"])
def test_wifi_reuses_ssid_and_rolls_back_replaced_password(monkeypatch, outcome):
    from copy import deepcopy

    dbus = pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    v = dbus.Variant
    settings = {
        "connection": {"uuid": v("s", "working"), "id": v("s", "phone")},
        "802-11-wireless": {"ssid": v("ay", b"phone")},
        "802-11-wireless-security": {"key-mgmt": v("s", "wpa-psk")},
        "ipv4": {"method": v("s", "manual")},
    }
    updates, activations, secrets = [], [], []

    class Profile:
        async def call_get_settings(self):
            return deepcopy(settings)

        async def call_get_secrets(self, key):
            secrets.append(key)
            return {key: {"psk": v("s", "old-secret")}}

        async def call_update(self, changed):
            updates.append(deepcopy(changed))
            if outcome == "update_error" and len(updates) == 1:
                raise RuntimeError("new-secret")

    class Manager:
        async def call_activate_connection(self, profile, path, specific):
            activations.append(profile)
            if outcome == "activate_error" and len(activations) == 1:
                raise RuntimeError("new-secret")
            return "/active/attempt" if len(activations) == 1 else "/active/old"

    async def device():
        return Manager(), "/device", {"ActiveConnection": "/active/old"}

    async def props(*args):
        return {"Connection": "/profile/working"}

    async def profiles():
        return [
            {"id": "failed", "ssid": "phone", "path": "/profile/failed", "last_connected": 999},
            {"id": "working", "ssid": "phone", "path": "/profile/working", "last_connected": 1},
        ]

    async def proxy(path, interface):
        assert path == "/profile/working"
        return Profile()

    async def wait(path, active):
        return outcome in ("success", "saved", "update_error") or active == "/active/old"

    wifi.device, wifi.props, wifi.profiles, wifi.proxy, wifi.wait_active = (
        device, props, profiles, proxy, wait
    )

    async def run():
        app = web.Application()
        app.router.add_post("/{action}", wifi.handle)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/connect", json={
                "ssid": "phone", "password": "" if outcome == "saved" else "new-secret"
            })
            data = await response.json()
            assert response.status == (200 if outcome in ("success", "saved") else
                                       409 if outcome == "failed" else 503)
            assert "new-secret" not in str(data) and "old-secret" not in str(data)
            if outcome not in ("success", "saved"):
                assert data["restored_previous"] is True
        assert activations and set(activations) == {"/profile/working"}
        if outcome == "saved":
            assert updates == [] and secrets == []
        else:
            assert updates[0]["connection"]["uuid"].value == "working"
            assert updates[0]["ipv4"]["method"].value == "manual"
            assert updates[0]["802-11-wireless-security"]["psk"].value == "new-secret"
            if outcome != "success":
                assert len(updates) == 2
                assert updates[1]["802-11-wireless-security"]["psk"].value == "old-secret"

    asyncio.run(run())


def test_wifi_forget_removes_all_saved_settings_for_only_requested_ssid(monkeypatch):
    pytest.importorskip("dbus_next")
    monkeypatch.setenv("GUIDE_WIFI_INTERFACE", "test-radio")
    wifi = load("wifi_service").Wifi()
    deleted = []

    class Profile:
        def __init__(self, path):
            self.path = path

        async def call_delete(self):
            deleted.append(self.path)

    async def device():
        return None, "/device", {}

    async def profiles():
        return [{"id": name, "ssid": ssid, "path": "/" + name}
                for name, ssid in [("one", "phone"), ("two", "phone"), ("other", "other")]]

    async def proxy(path, interface):
        return Profile(path)

    async def state():
        return {"profiles": []}

    wifi.device, wifi.profiles, wifi.proxy, wifi.state = device, profiles, proxy, state

    async def run():
        app = web.Application()
        app.router.add_post("/{action}", wifi.handle)
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/forget", json={"ssid": "phone"})).status == 200
        assert deleted == ["/one", "/two"]

    asyncio.run(run())
