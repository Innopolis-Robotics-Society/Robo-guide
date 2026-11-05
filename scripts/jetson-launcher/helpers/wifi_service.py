"""Restricted NetworkManager D-Bus facade for one configured non-hotspot adapter."""

import asyncio
import os
import uuid
from pathlib import Path

from aiohttp import web
from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

NM = "org.freedesktop.NetworkManager"
ROOT = "/org/freedesktop/NetworkManager"


class Wifi:
    """Restricted system service with explicit device ownership."""

    def __init__(self):
        """Initialize the adapter and its configured resources."""
        self.interface = os.environ["GUIDE_WIFI_INTERFACE"]
        if self.interface == os.environ.get("GUIDE_WIFI_HOTSPOT_INTERFACE"):
            raise ValueError("Service hotspot adapter is excluded")
        # USB adapters are reserved for maintenance in this deployment.
        if "/usb" in str(Path("/sys/class/net", self.interface).resolve()):
            raise ValueError("USB maintenance adapter is excluded")
        self.bus = None
        self.lock = asyncio.Lock()
        self.phase = "connecting"

    async def proxy(self, path, interface):
        """Resolve a typed NetworkManager D-Bus interface."""
        intro = await self.bus.introspect(NM, path)
        return self.bus.get_proxy_object(NM, path, intro).get_interface(interface)

    async def props(self, path, interface):
        """Read interface properties without requesting secrets."""
        props = await self.proxy(path, "org.freedesktop.DBus.Properties")
        return {k: v.value for k, v in (await props.call_get_all(interface)).items()}

    async def device(self):
        """Resolve only the configured non-hotspot adapter."""
        if self.bus is None:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        manager = await self.proxy(ROOT, NM)
        path = await manager.call_get_device_by_ip_iface(self.interface)
        props = await self.props(path, NM + ".Device")
        if props["DeviceType"] != 2:
            raise ValueError("not_wifi")
        return manager, path, props

    async def profiles(self):
        """List compatible profiles without requesting secrets."""
        settings = await self.proxy(ROOT + "/Settings", NM + ".Settings")
        result = []
        for path in await settings.call_list_connections():
            profile = await self.proxy(path, NM + ".Settings.Connection")
            data = await profile.call_get_settings()
            conn = data["connection"]
            if (
                conn["type"].value != "802-11-wireless"
                or conn.get("interface-name", Variant("s", "")).value != self.interface
            ):
                continue
            security = data.get("802-11-wireless-security", {})
            if security.get("key-mgmt", Variant("s", "none")).value not in (
                "none",
                "wpa-psk",
                "sae",
            ):
                continue
            wifi = data.get("802-11-wireless", {})
            if wifi.get("mode", Variant("s", "infrastructure")).value != "infrastructure":
                continue
            result.append(
                {
                    "id": conn["uuid"].value,
                    "ssid": bytes(wifi["ssid"].value).decode("utf-8", "replace"),
                    "path": path,
                }
            )
        return result

    async def state(self):
        """Read public map or network state without credentials."""
        _, path, props = await self.device()
        wireless = await self.proxy(path, NM + ".Device.Wireless")
        networks = []
        for ap in await wireless.call_get_all_access_points():
            item = await self.props(ap, NM + ".AccessPoint")
            flags = item.get("RsnFlags", 0) | item.get("WpaFlags", 0)
            enterprise = bool(flags & 0x200)
            security = (
                "enterprise"
                if enterprise
                else "personal"
                if flags & (0x100 | 0x400)
                else "open"
                if not item["Flags"]
                else "unsupported"
            )
            networks.append(
                {
                    "ssid": bytes(item["Ssid"]).decode("utf-8", "replace"),
                    "strength": item["Strength"],
                    "security": security,
                    "sae_only": bool(flags & 0x400) and not bool(flags & 0x100),
                }
            )
        wireless_state = await self.props(path, NM + ".Device.Wireless")
        active_ap = wireless_state.get("ActiveAccessPoint", "/")
        current_ssid = None
        if active_ap != "/":
            current_ssid = bytes(
                (await self.props(active_ap, NM + ".AccessPoint"))["Ssid"]
            ).decode("utf-8", "replace")
        return {
            "current_ssid": current_ssid,
            "available": True,
            "device": self.interface,
            "state": props["State"],
            "networks": networks,
            "profiles": [
                {k: v for k, v in p.items() if k != "path"} for p in await self.profiles()
            ],
        }

    async def wait_active(self, path, active):
        """Wait for the requested connection rather than a previous connection."""
        for _ in range(60):
            observed = await self.props(path, NM + ".Device")
            state = observed["State"]
            if self.phase != "restoring":
                self.phase = ("authenticating" if state in (50, 60) else
                              "obtaining_address" if state in (70, 80, 90) else "connecting")
            if state == 100 and observed["ActiveConnection"] == active:
                return True
            if state == 120:
                return False
            await asyncio.sleep(1)
        return False

    async def handle(self, request):
        """Validate and execute a restricted helper request."""
        action = request.match_info["action"]
        if request.method == "GET" and action == "progress":
            return web.json_response({"phase": self.phase})
        if request.method != "POST" and action != "status":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        if self.lock.locked():
            return web.json_response({"message": "network_busy"}, status=409)
        async with self.lock:
            previous = None
            rollback_attempted = False
            try:
                manager, path, props = await self.device()
                if request.method == "GET" and action == "status":
                    return web.json_response(await self.state())
                body = await request.json() if request.can_read_body else {}
                if action == "scan":
                    wireless = await self.proxy(path, NM + ".Device.Wireless")
                    before = (await self.props(path, NM + ".Device.Wireless"))["LastScan"]
                    await wireless.call_request_scan({})
                    # RequestScan only accepts the request. LastScan changes when
                    # NetworkManager has completed discovery, not after a fixed delay.
                    deadline = asyncio.get_running_loop().time() + 25
                    while (await self.props(path, NM + ".Device.Wireless"))["LastScan"] <= before:
                        if asyncio.get_running_loop().time() >= deadline:
                            return web.json_response({"message": "scan_timeout"}, status=504)
                        await asyncio.sleep(0.25)
                    return web.json_response(await self.state())
                profiles = await self.profiles()
                selected = next((p for p in profiles if p["id"] == body.get("profile_id")), None)
                if action == "disconnect":
                    device = await self.proxy(path, NM + ".Device")
                    await device.call_disconnect()
                elif action == "forget":
                    if selected is None:
                        raise ValueError("profile_not_found")
                    profile = await self.proxy(selected["path"], NM + ".Settings.Connection")
                    await profile.call_delete()
                elif action == "connect":
                    self.phase = "connecting"
                    previous = None
                    if props["ActiveConnection"] != "/":
                        previous = (
                            await self.props(props["ActiveConnection"], NM + ".Connection.Active")
                        )["Connection"]
                    if selected:
                        active = await manager.call_activate_connection(
                            selected["path"], path, "/"
                        )
                    else:
                        ssid, password = body.get("ssid", ""), body.get("password", "")
                        if (
                            not isinstance(ssid, str)
                            or not 1 <= len(ssid.encode()) <= 32
                            or not isinstance(password, str)
                        ):
                            raise ValueError("invalid_network")
                        if password and not (
                            8 <= len(password) <= 63
                            or len(password) == 64
                            and all(c in "0123456789abcdefABCDEF" for c in password)
                        ):
                            raise ValueError("invalid_password")
                        networks = (await self.state())["networks"]
                        network = next((n for n in networks if n["ssid"] == ssid), None)
                        if network and network["security"] in ("enterprise", "unsupported"):
                            raise ValueError("unsupported_security")
                        settings = {
                            "connection": {
                                "id": Variant("s", "guide-" + ssid),
                                "uuid": Variant("s", str(uuid.uuid4())),
                                "type": Variant("s", "802-11-wireless"),
                                "interface-name": Variant("s", self.interface),
                            },
                            "802-11-wireless": {
                                "ssid": Variant("ay", ssid.encode()),
                                "mode": Variant("s", "infrastructure"),
                                "hidden": Variant("b", bool(body.get("hidden", False))),
                            },
                            "ipv4": {"method": Variant("s", "auto")},
                            "ipv6": {"method": Variant("s", "auto")},
                        }
                        if password:
                            settings["802-11-wireless-security"] = {
                                "key-mgmt": Variant(
                                    "s",
                                    "sae" if network and network.get("sae_only") else "wpa-psk",
                                ),
                                "psk": Variant("s", password),
                            }
                        _, active = await manager.call_add_and_activate_connection(
                            settings, path, "/"
                        )
                    connected = await self.wait_active(path, active)
                    restored = False
                    if not connected and previous:
                        self.phase = "restoring"
                        rollback_attempted = True
                        fallback = await manager.call_activate_connection(previous, path, "/")
                        restored = await self.wait_active(path, fallback)
                    return web.json_response(
                        {"connected": connected, "restored_previous": restored},
                        status=200 if connected else 409,
                    )
                else:
                    raise ValueError("unsupported_action")
                return web.json_response(await self.state())
            except ValueError as exc:
                return web.json_response({"message": str(exc)}, status=400)
            except Exception:
                # D-Bus errors may include a credential: never log exception text.
                restored = False
                if action == "connect" and previous and not rollback_attempted:
                    self.phase = "restoring"
                    try:
                        fallback = await manager.call_activate_connection(previous, path, "/")
                        restored = await self.wait_active(path, fallback)
                    except Exception:
                        pass
                return web.json_response(
                    {"message": "network_unavailable", "restored_previous": restored}, status=503
                )


if __name__ == "__main__":
    os.umask(0o007)
    wifi = Wifi()
    app = web.Application()
    app.router.add_get("/{action}", wifi.handle)
    app.router.add_post("/{action}", wifi.handle)
    web.run_app(app, path=os.environ["GUIDE_WIFI_SOCKET"])
