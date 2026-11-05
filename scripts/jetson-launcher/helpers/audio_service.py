"""Run in the user's PulseAudio session; only the configured speaker is mutable."""

import asyncio
import json
import os
from pathlib import Path

from aiohttp import web


class Audio:
    """Restricted system service with explicit device ownership."""

    def __init__(self):
        """Initialize the adapter and its configured resources."""
        self.sink = os.environ["GUIDE_AUDIO_SINK"]
        self.maximum = float(os.environ.get("GUIDE_AUDIO_MAX_PERCENT", "20"))
        if not 0 < self.maximum <= 100:
            raise ValueError("Invalid speaker limit")
        self.path = Path(
            os.environ.get("GUIDE_AUDIO_STATE", str(Path.home() / ".local/state/guide-audio.json"))
        )
        self.desired = {"volume": 100.0, "muted": False}
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            value = saved.get("volume")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= value <= 100
                and isinstance(saved.get("muted"), bool)
            ):
                self.desired.update(volume=value, muted=saved["muted"])
        self.lock = asyncio.Lock()
        self.present = False

    async def pactl(self, *args):
        """Invoke the audio utility without a shell and with a deadline."""
        proc = await asyncio.create_subprocess_exec(
            "pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("audio_timeout") from None
        if proc.returncode:
            raise RuntimeError("audio_unavailable")
        return out

    async def read(self):
        """Read the actual configured speaker state."""
        sinks = json.loads(await self.pactl("--format=json", "list", "sinks"))
        sink = next((s for s in sinks if s["name"] == self.sink), None)
        if sink is None:
            raise RuntimeError("audio_unavailable")
        channels = list(sink["volume"].values())
        percent = max(float(c["value_percent"].rstrip("%")) for c in channels)
        return {
            "available": True,
            "volume": min(100, percent / self.maximum * 100),
            "muted": sink["mute"],
            "maximum_percent": self.maximum,
        }

    async def apply(self, desired):
        """Set the permitted hardware gain and mute state."""
        await self.pactl(
            "set-sink-volume", self.sink, str(desired["volume"] * self.maximum / 100) + "%"
        )
        await self.pactl("set-sink-mute", self.sink, "1" if desired["muted"] else "0")

    async def handle(self, request):
        """Validate and execute a restricted helper request."""
        async with self.lock:
            try:
                if request.method == "POST":
                    body = await request.json()
                    if set(body) - {"volume", "muted"}:
                        raise ValueError("invalid_arguments")
                    desired = {**self.desired, **body}
                    volume = desired["volume"]
                    if (
                        isinstance(volume, bool)
                        or not isinstance(volume, (int, float))
                        or not 0 <= volume <= 100
                        or not isinstance(desired["muted"], bool)
                    ):
                        raise ValueError("invalid_arguments")
                    await self.apply(desired)
                    self.desired = desired
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(desired))
                    temporary.replace(self.path)
                return web.json_response(await self.read())
            except ValueError:
                return web.json_response({"message": "invalid_arguments"}, status=400)
            except RuntimeError:
                return web.json_response({"message": "audio_unavailable"}, status=503)

    async def restore(self, app):
        """Restore the last requested settings when a speaker appears."""

        async def run():
            while True:
                async with self.lock:
                    try:
                        await self.read()
                        if not self.present:
                            await self.apply(self.desired)
                        self.present = True
                    except RuntimeError:
                        self.present = False
                await asyncio.sleep(2)

        task = asyncio.create_task(run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    os.umask(0o007)
    audio = Audio()
    app = web.Application()
    app.router.add_get("/output", audio.handle)
    app.router.add_post("/output", audio.handle)
    app.cleanup_ctx.append(audio.restore)
    web.run_app(app, path=os.environ["GUIDE_AUDIO_SOCKET"])
