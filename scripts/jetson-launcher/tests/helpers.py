from __future__ import annotations

import asyncio
from pathlib import Path

from guide_launcher.config import Config
from guide_launcher.stack import RunResult


class Clock:
    """Управляемое время: sleep() двигает часы, а не ждёт."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds
        await asyncio.sleep(0)


class FakeDocker:
    """Скриптуемый docker: разбирает argv и меняет состояние контейнера/launch."""

    def __init__(self, container: str = "c") -> None:
        self.name = container
        self.container_exists = True
        self.running = True
        self.launch_alive = True
        self.pgrep_alive_polls = 0
        self.fail: set[str] = set()
        self.calls: list[list[str]] = []
        self.tail_text = "line1\nline2\n"

    async def __call__(self, argv: list[str], timeout: float) -> RunResult:
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""
        if verb in self.fail:
            return RunResult(1, "boom")
        if verb == "inspect":
            if not self.container_exists:
                return RunResult(1, "No such object")
            return RunResult(0, "true\n" if self.running else "false\n")
        if verb == "start":
            self.running = True
            return RunResult(0, "")
        if verb == "restart":
            self.running = True
            self.launch_alive = False
            return RunResult(0, "")
        if verb == "exec":
            return self._exec(argv)
        return RunResult(2, "unexpected")

    def _exec(self, argv: list[str]) -> RunResult:
        if argv[2] == "-d":
            self.launch_alive = True
            return RunResult(0, "")
        cmd = argv[3:]
        if not self.running:
            return RunResult(1, "not running")
        if cmd[0] == "pgrep":
            if self.pgrep_alive_polls > 0:
                self.pgrep_alive_polls -= 1
                if self.pgrep_alive_polls == 0:
                    self.launch_alive = False
                return RunResult(0, "1\n")
            return RunResult(0 if self.launch_alive else 1, "")
        if cmd[0] == "pkill":
            return RunResult(0, "")
        if cmd[0] == "tail":
            return RunResult(0, self.tail_text)
        return RunResult(2, "unexpected")

    def verbs(self) -> list[str]:
        """Последовательность команд в виде 'inspect', 'exec:pkill', 'exec:-d' и т.п."""
        out = []
        for argv in self.calls:
            if argv[1] == "exec":
                out.append("exec:-d" if argv[2] == "-d" else f"exec:{argv[3]}")
            else:
                out.append(argv[1])
        return out


def make_cfg(tmp_path: Path, **overrides) -> Config:
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<html>launcher</html>", encoding="utf-8")
    (web / "app.js").write_text("// js", encoding="utf-8")
    base = {
        "container": "c",
        "start_cmd": "/x/start_stack.sh",
        "stack_log": "/tmp/stack.log",
        "promo_dir": str(tmp_path / "promo"),
        "web_dir": str(web),
        "operator_pin": "12345678",
        "start_timeout_s": 120.0,
        "poll_interval_s": 2.0,
    }
    base.update(overrides)
    return Config(**base)
