from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from guide_launcher.stack import PROBE_FAILS_TO_DROP, StackError, StackMonitor, StackState
from helpers import Clock, FakeDocker, make_cfg


def _monitor(tmp_path: Path, docker: FakeDocker, bridge_up: list[bool], clock: Clock, **kw):
    cfg = make_cfg(tmp_path, **kw.pop("cfg", {}))

    async def probe() -> bool:
        return bridge_up[0]

    return StackMonitor(cfg, probe, run=docker, clock=clock, sleep=clock.sleep, **kw)


def test_down_when_container_stopped(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.running = False
    m = _monitor(tmp_path, d, [False], clock)
    assert asyncio.run(m.poll()) is StackState.DOWN
    assert (m.checks.container, m.checks.launch) == (False, False)
    assert "pgrep" not in " ".join(d.verbs())


def test_down_when_launch_dead(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.launch_alive = False
    m = _monitor(tmp_path, d, [False], clock)
    assert asyncio.run(m.poll()) is StackState.DOWN
    assert m.checks.container and not m.checks.launch


def test_up_when_all_checks_pass(tmp_path):
    m = _monitor(tmp_path, FakeDocker(), [True], Clock())
    assert asyncio.run(m.poll()) is StackState.UP
    assert m.status()["checks"] == {"container": True, "launch": True, "bridge": True}


def test_mismatch_is_starting_then_degraded_then_up(tmp_path):
    clock, bridge = Clock(), [False]
    m = _monitor(tmp_path, FakeDocker(), bridge, clock)

    async def go():
        assert await m.poll() is StackState.STARTING
        clock.t += 119
        assert await m.poll() is StackState.STARTING
        clock.t += 2
        assert await m.poll() is StackState.DEGRADED
        bridge[0] = True
        assert await m.poll() is StackState.UP

    asyncio.run(go())


def test_start_boots_stopped_container_and_execs_start_cmd(tmp_path):
    d, clock, bridge = FakeDocker(), Clock(), [False]
    d.running = False
    d.launch_alive = False
    m = _monitor(tmp_path, d, bridge, clock)

    async def go():
        await m.poll()
        assert m.state is StackState.DOWN
        await m.start()
        assert m.state is StackState.STARTING
        assert await m.poll() is StackState.STARTING
        bridge[0] = True
        assert await m.poll() is StackState.UP

    asyncio.run(go())
    verbs = d.verbs()
    assert verbs.index("start") < verbs.index("exec:-d")
    assert ["docker", "start", "c"] in d.calls
    exec_call = next(c for c in d.calls if c[:3] == ["docker", "exec", "-d"])
    assert exec_call == [
        "docker", "exec", "-d", "-e", "STACK_LOG=/tmp/stack.log", "c", "/x/start_stack.sh"
    ]  # fmt: skip


def test_start_skips_docker_start_when_container_running(tmp_path):
    d = FakeDocker()
    d.launch_alive = False
    m = _monitor(tmp_path, d, [False], Clock())
    asyncio.run(m.start())
    assert ["docker", "start", "c"] not in d.calls


def test_starting_timeout_is_failed_and_holds_until_up(tmp_path):
    d, clock, bridge = FakeDocker(), Clock(), [False]
    m = _monitor(tmp_path, d, bridge, clock)

    async def go():
        await m.start()
        clock.t += 121
        assert await m.poll() is StackState.FAILED
        assert "timeout" in m.last_error
        clock.t += 500
        assert await m.poll() is StackState.FAILED
        d.launch_alive = False
        assert await m.poll() is StackState.FAILED
        d.launch_alive = True
        bridge[0] = True
        assert await m.poll() is StackState.UP
        assert m.last_error == ""

    asyncio.run(go())


def test_new_command_leaves_failed(tmp_path):
    d, clock = FakeDocker(), Clock()
    m = _monitor(tmp_path, d, [False], clock)

    async def go():
        await m.start()
        clock.t += 121
        await m.poll()
        assert m.state is StackState.FAILED
        await m.start()
        assert m.state is StackState.STARTING

    asyncio.run(go())


def test_repeat_command_during_starting_is_409(tmp_path):
    m = _monitor(tmp_path, FakeDocker(), [False], Clock())

    async def go():
        await m.start()
        for call in (m.start, m.restart):
            with pytest.raises(StackError) as exc:
                await call()
            assert exc.value.status == 409

    asyncio.run(go())


def test_concurrent_start_only_one_wins(tmp_path):
    d = FakeDocker()
    m = _monitor(tmp_path, d, [False], Clock())

    async def go():
        return await asyncio.gather(m.start(), m.start(), return_exceptions=True)

    results = asyncio.run(go())
    assert sum(isinstance(r, StackError) and r.status == 409 for r in results) == 1
    assert sum(c[:3] == ["docker", "exec", "-d"] for c in d.calls) == 1


def test_restart_order_graceful_stop_then_docker_restart_then_start(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.pgrep_alive_polls = 3
    m = _monitor(tmp_path, d, [False], clock)
    asyncio.run(m.restart())
    assert d.verbs() == [
        "exec:pkill", "exec:pgrep", "exec:pgrep", "exec:pgrep", "exec:pgrep", "restart", "exec:-d"
    ]  # fmt: skip
    assert d.calls[0] == ["docker", "exec", "c", "pkill", "-INT", "-f", "ros2 launch"]
    assert ["docker", "restart", "-t", "20", "c"] in d.calls
    assert clock.slept == pytest.approx(1.5)


def test_restart_waits_at_most_20s_for_launch_to_exit(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.pgrep_alive_polls = 10_000
    m = _monitor(tmp_path, d, [False], clock)
    asyncio.run(m.restart())
    assert 20.0 <= clock.slept <= 20.5
    assert d.verbs()[-2:] == ["restart", "exec:-d"]


def test_restart_guard_refuses_without_touching_docker(tmp_path):
    d = FakeDocker()
    m = _monitor(tmp_path, d, [True], Clock(), restart_guard=lambda: "robot_moving")

    async def go():
        await m.poll()
        d.calls.clear()
        with pytest.raises(StackError) as exc:
            await m.restart()
        assert (exc.value.status, exc.value.reason) == (409, "robot_moving")
        assert m.state is StackState.UP

    asyncio.run(go())
    assert d.calls == []


def test_restart_guard_none_allows_restart(tmp_path):
    d = FakeDocker()
    m = _monitor(tmp_path, d, [False], Clock(), restart_guard=lambda: None)
    asyncio.run(m.restart())
    assert "restart" in d.verbs()


def test_docker_failure_marks_failed_with_error(tmp_path):
    d = FakeDocker()
    d.running = False
    d.fail.add("start")
    m = _monitor(tmp_path, d, [False], Clock())
    with pytest.raises(StackError) as exc:
        asyncio.run(m.start())
    assert exc.value.status == 500
    assert m.state is StackState.FAILED
    assert "docker start" in m.last_error


def test_control_disabled_mode_uses_bridge_only(tmp_path):
    d, bridge = FakeDocker(), [True]
    m = _monitor(tmp_path, d, bridge, Clock(), cfg={"container": ""})

    async def go():
        assert await m.poll() is StackState.UP
        bridge[0] = False
        for _ in range(PROBE_FAILS_TO_DROP - 1):
            assert await m.poll() is StackState.UP
        assert await m.poll() is StackState.DOWN
        for call in (m.start, m.restart):
            with pytest.raises(StackError) as exc:
                await call()
            assert exc.value.status == 400
        assert await m.log_tail() == ""

    asyncio.run(go())
    assert d.calls == []
    assert m.control is False


def test_log_tail_reads_last_30_lines(tmp_path):
    d = FakeDocker()
    m = _monitor(tmp_path, d, [True], Clock())
    assert asyncio.run(m.log_tail()) == "line1\nline2\n"
    assert d.calls[-1] == ["docker", "exec", "c", "tail", "-n", "30", "/tmp/stack.log"]


def _run_forever(monitor: StackMonitor, clock: Clock, iterations: int) -> None:
    count = 0
    real_sleep = clock.sleep

    async def limited_sleep(seconds: float) -> None:
        nonlocal count
        count += 1
        if count >= iterations:
            raise asyncio.CancelledError
        await real_sleep(seconds)

    monitor._sleep = limited_sleep
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(monitor.run_forever())


def test_autostart_starts_once_when_down(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.launch_alive = False
    m = _monitor(tmp_path, d, [False], clock, cfg={"autostart_stack": True})
    _run_forever(m, clock, 4)
    assert sum(c[:3] == ["docker", "exec", "-d"] for c in d.calls) == 1


def test_autostart_disabled_by_default(tmp_path):
    d, clock = FakeDocker(), Clock()
    d.launch_alive = False
    m = _monitor(tmp_path, d, [False], clock)
    _run_forever(m, clock, 3)
    assert not any(c[:3] == ["docker", "exec", "-d"] for c in d.calls)


def test_autostart_does_not_touch_already_running_stack(tmp_path):
    d, clock = FakeDocker(), Clock()
    m = _monitor(tmp_path, d, [True], clock, cfg={"autostart_stack": True})
    _run_forever(m, clock, 3)
    assert not any(c[:3] == ["docker", "exec", "-d"] for c in d.calls)


def test_up_survives_failed_probes_until_three_in_a_row(tmp_path):
    bridge = [True]
    m = _monitor(tmp_path, FakeDocker(), bridge, Clock())

    async def go():
        assert await m.poll() is StackState.UP
        bridge[0] = False
        for _ in range(PROBE_FAILS_TO_DROP - 1):
            assert await m.poll() is StackState.UP
        bridge[0] = True
        assert await m.poll() is StackState.UP
        bridge[0] = False
        for _ in range(PROBE_FAILS_TO_DROP - 1):
            assert await m.poll() is StackState.UP
        assert await m.poll() is StackState.STARTING
        assert m.checks.bridge is False

    asyncio.run(go())


def test_up_drops_immediately_when_container_or_launch_dies(tmp_path):
    d, bridge = FakeDocker(), [True]
    m = _monitor(tmp_path, d, bridge, Clock())

    async def go():
        assert await m.poll() is StackState.UP
        bridge[0] = False
        d.launch_alive = False
        assert await m.poll() is StackState.DOWN

    asyncio.run(go())
