"""Regressions found by the independent API implementation review."""

from test_api import env, run
from test_v1 import settled, start


def test_terminal_operation_cannot_regress_or_lose_result(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            op = await (await start(e)).json()
            await settled(e)
            entry = e.app["api"].events.operations[op["id"]]
            entry.update(status="succeeded", result={"detail": "done"})
            e.set_frame(operations=[{"id": op["id"], "status": "running", "result": None}])
            e.app["api"].events.reconcile()
            assert entry["status"] == "succeeded" and entry["result"] == {"detail": "done"}

    run(go())


def test_quick_reconnect_marks_old_operation_unknown_before_new_reservation(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            old = await (await start(e)).json()
            await settled(e)
            e.watcher.generation += 1
            e.set_frame(operations=[{"id": old["id"], "status": "running"}])
            ws = await e.client.ws_connect("/api/v1/events")
            snapshot = await ws.receive_json(timeout=1)
            assert snapshot["data"]["operations"][0]["status"] == "unknown"
            await ws.close()
            new = await (await start(e, key="two")).json()
            assert new["status"] == "pending"

    run(go())


def test_mission_age_includes_host_elapsed_time(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(mission_state_age_s=2)
            e.clock.t += 1.5
            assert e.watcher.latest().data["mission_state_age_s"] == 3.5
            assert e.app["api"].restart_guard() == "mission_state_stale"

    run(go())


def test_launch_missing_does_not_prove_stopped_container(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            await e.backend.close()
            e.docker.launch_alive = False
            await e.monitor.poll()
            assert e.monitor.confirmed_stopped_at is None
            assert e.app["api"].restart_guard() == "mission_state_stale"

    run(go())


def test_restart_rechecks_ros_ownership_and_blocks_commands_during_transition(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            # Last heartbeat can be idle while a standalone return is being accepted.
            e.set_frame()
            calls = []

            async def bridge(path, body, operator):
                calls.append(path)
                return 409, {"success": False, "message": "mission_not_idle"}

            e.app["api"].bridge.command = bridge
            assert (await e.client.post("/api/v1/stack/restart")).status == 409
            assert calls == ["/internal/missions/check-idle"]
            assert not e.docker.calls
            async with e.monitor._lock:
                # A concurrent readiness poll must not unfence an ongoing restart.
                await e.monitor.poll()
                assert e.monitor.transitioning
                assert (await e.client.post("/api/v1/missions/return")).status == 409
            assert calls == ["/internal/missions/check-idle"]

    run(go())


def test_no_partial_preferences_write_on_invalid_request(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            response = await e.client.post(
                "/api/v1/reference/preferences",
                json={"tour_id": "changed", "confirm_between_stops": "invalid"},
            )
            assert response.status == 400
            prefs = await (await e.client.get("/api/v1/reference/preferences")).json()
            assert prefs["tour_id"] == "expo_one"

    run(go())


def test_answering_on_the_move_allows_public_stop(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name="answering", moving=True)
            assert (await e.client.post("/api/v1/public/stop")).status == 200

    run(go())


def test_host_header_cannot_make_remote_origin_local(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            response = await e.client.get(
                "/api/v1/health", headers={"Host": "evil.test", "Origin": "http://evil.test"}
            )
            assert response.status == 403

    run(go())


def test_lost_action_server_marks_operation_unknown(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            op = await (await start(e)).json()
            await settled(e)
            e.set_frame(
                readiness={"mission": False}, operations=[{"id": op["id"], "status": "running"}]
            )
            response = await e.client.get("/api/v1/operations/" + op["id"])
            assert (await response.json())["status"] == "unknown"

    run(go())


def test_public_timeout_is_unknown_and_retains_observed_state(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            e.set_frame(state_name="navigating")

            async def uncertain(*args):
                return 504, {"message": "outcome_unknown"}

            e.app["api"].bridge.command = uncertain
            response = await e.client.post("/api/v1/public/stop")
            data = await response.json()
            assert response.status == 504
            assert data["error"]["code"] == "outcome_unknown"
            assert data["state"]["robot"]["state_name"] == "navigating"

    run(go())
