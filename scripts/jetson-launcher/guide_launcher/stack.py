"""Состояние ROS-стека в docker-контейнере и команды start/restart."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import Enum
from typing import NamedTuple

from .config import Config

log = logging.getLogger(__name__)

LAUNCH_PATTERN = "ros2 launch"
PROBE_FAILS_TO_DROP = 3
GRACEFUL_STOP_WAIT_S = 20.0
DOCKER_RESTART_GRACE_S = 20


class StackState(str, Enum):
    """Итоговое состояние стека для страницы и меню."""

    DOWN = "DOWN"
    STARTING = "STARTING"
    UP = "UP"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class RunResult(NamedTuple):
    """Итог внешней команды: код возврата и stdout+stderr."""

    rc: int
    out: str


RunFn = Callable[[list[str], float], Awaitable[RunResult]]
ProbeFn = Callable[[], Awaitable[bool]]
SleepFn = Callable[[float], Awaitable[None]]


class StackError(Exception):
    """Команда стека не принята (HTTP-код и причина для ответа)."""

    def __init__(self, status: int, reason: str) -> None:
        """`status` -- HTTP-код ответа, `reason` -- машинная причина."""
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class Checks:
    """Три независимые проверки из TASK_launcher.md."""

    container: bool = False
    launch: bool = False
    bridge: bool = False


async def default_run(argv: list[str], timeout: float) -> RunResult:
    """Выполнить команду без shell с таймаутом; ошибки запуска -- в rc, не исключением."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return RunResult(127, str(exc))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return RunResult(-1, f"timeout after {timeout}s")
    return RunResult(
        proc.returncode if proc.returncode is not None else -1, out.decode(errors="replace")
    )


class StackMonitor:
    """Опрашивает container/launch/bridge и выполняет start/restart под одним lock."""

    def __init__(
        self,
        cfg: Config,
        probe: ProbeFn,
        *,
        run: RunFn = default_run,
        clock: Callable[[], float] = time.monotonic,
        sleep: SleepFn = asyncio.sleep,
        restart_guard: Callable[[], str | None] | None = None,
    ) -> None:
        """`restart_guard` возвращает причину отказа (str) или None, если рестарт разрешён."""
        self._cfg = cfg
        self._probe = probe
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self.restart_guard = restart_guard
        self._lock = asyncio.Lock()
        self._starting_since: float | None = None
        self._mismatch_since: float | None = None
        self._probe_fails = 0
        self.state = StackState.DOWN
        self.checks = Checks()
        self.last_error = ""

    @property
    def control(self) -> bool:
        """Управление стеком включено (задан container)."""
        return self._cfg.stack_control

    async def poll(self) -> StackState:
        """Один цикл проверок и пересчёт состояния."""
        checks = Checks()
        if self.control:
            checks.container = await self._container_running()
            if checks.container:
                checks.launch = (await self._exec(["pgrep", "-f", LAUNCH_PATTERN], 4.0)).rc == 0
        else:
            checks.container = checks.launch = True
        bridge_ok = await self._probe()
        self._probe_fails = 0 if bridge_ok else self._probe_fails + 1
        debounced = self.state is StackState.UP and self._probe_fails < PROBE_FAILS_TO_DROP
        checks.bridge = bridge_ok or debounced
        self.checks = checks
        self.state = self._derive(checks)
        return self.state

    async def run_forever(self) -> None:
        """Опрос раз в poll_interval_s; autostart_stack срабатывает один раз после первого."""
        first = True
        while True:
            try:
                await self.poll()
                autostart = first and self._cfg.autostart_stack and self.control
                if autostart and self.state is StackState.DOWN:
                    await self.start()
            except StackError as exc:
                log.error("autostart: %s (%s)", exc.reason, self.last_error)
            except Exception:
                log.exception("poll failed")
            first = False
            await self._sleep(self._cfg.poll_interval_s)

    def _derive(self, c: Checks) -> StackState:
        now = self._clock()
        if c.container and c.launch and c.bridge:
            self._starting_since = None
            self._mismatch_since = None
            self.last_error = ""
            return StackState.UP
        if self._starting_since is not None:
            if now - self._starting_since > self._cfg.start_timeout_s:
                self._starting_since = None
                self.last_error = self.last_error or "start timeout: bridge did not come up"
                return StackState.FAILED
            return StackState.STARTING
        if self.state is StackState.FAILED:
            return StackState.FAILED
        if not self.control:
            return StackState.DOWN
        if not (c.container and c.launch):
            self._mismatch_since = None
            return StackState.DOWN
        if self._mismatch_since is None:
            self._mismatch_since = now
        if now - self._mismatch_since > self._cfg.start_timeout_s:
            return StackState.DEGRADED
        return StackState.STARTING

    async def start(self) -> None:
        """Запустить контейнер (если нужно) и выполнить start_cmd внутри."""
        self._begin()
        async with self._lock:
            if not await self._container_running():
                await self._docker(["docker", "start", self._cfg.container], 60.0, "docker start")
            await self._exec_start()

    async def restart(self) -> None:
        """Мягко остановить launch, docker restart -t 20, затем start_cmd."""
        if not self.control:
            raise StackError(400, "stack_control_disabled")
        reason = self.restart_guard() if self.restart_guard else None
        if reason:
            raise StackError(409, reason)
        self._begin()
        async with self._lock:
            await self._graceful_stop()
            await self._docker(
                ["docker", "restart", "-t", str(DOCKER_RESTART_GRACE_S), self._cfg.container],
                90.0,
                "docker restart",
            )
            await self._exec_start()

    async def log_tail(self, lines: int = 30) -> str:
        """Последние строки stack_log из контейнера (пусто, если недоступен)."""
        if not self.control:
            return ""
        res = await self._exec(["tail", "-n", str(lines), self._cfg.stack_log], 5.0)
        return res.out if res.rc == 0 else ""

    def status(self) -> dict:
        """Снимок для /api/stack/status."""
        return {
            "state": self.state.value,
            "checks": asdict(self.checks),
            "control": self.control,
            "last_error": self.last_error,
        }

    def _begin(self) -> None:
        if not self.control:
            raise StackError(400, "stack_control_disabled")
        if self._starting_since is not None or self._lock.locked():
            raise StackError(409, "starting")
        self._starting_since = self._clock()
        self._mismatch_since = None
        self.state = StackState.STARTING
        self.last_error = ""

    async def _graceful_stop(self) -> None:
        name = self._cfg.container
        await self._exec(["pkill", "-INT", "-f", LAUNCH_PATTERN], 10.0)
        deadline = self._clock() + GRACEFUL_STOP_WAIT_S
        while (await self._exec(["pgrep", "-f", LAUNCH_PATTERN], 5.0)).rc == 0:
            if self._clock() >= deadline:
                log.warning("%s: ros2 launch не завершился за %.0f с", name, GRACEFUL_STOP_WAIT_S)
                return
            await self._sleep(0.5)

    async def _exec_start(self) -> None:
        cfg = self._cfg
        argv = ["docker", "exec", "-d", "-e", f"STACK_LOG={cfg.stack_log}"]
        await self._docker([*argv, cfg.container, cfg.start_cmd], 30.0, "docker exec start_cmd")

    async def _container_running(self) -> bool:
        argv = ["docker", "inspect", "-f", "{{.State.Running}}", self._cfg.container]
        res = await self._run(argv, 4.0)
        return res.rc == 0 and res.out.strip() == "true"

    async def _exec(self, cmd: list[str], timeout: float) -> RunResult:
        return await self._run(["docker", "exec", self._cfg.container, *cmd], timeout)

    async def _docker(self, argv: list[str], timeout: float, what: str) -> None:
        res = await self._run(argv, timeout)
        if res.rc != 0:
            self._starting_since = None
            self.state = StackState.FAILED
            self.last_error = f"{what}: rc={res.rc} {res.out.strip()[:200]}"
            raise StackError(500, "command_failed")
