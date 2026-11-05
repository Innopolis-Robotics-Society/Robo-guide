"""Public contract, ownership, idempotency and recovery against private ROS mocks."""

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from test_api import _public, env, run

SPEC = Path(__file__).parents[1] / "guide_launcher/spec"
OPENAPI = json.loads((SPEC / "openapi.json").read_text())


def validate(name, value):
    jsonschema.validate(
        value, {**OPENAPI["components"]["schemas"][name], "components": OPENAPI["components"]}
    )


async def start(e, kind="missions", key="one", body=None):
    return await e.client.post(
        "/api/v1/" + kind, json=body or {"tour_id": "expo_two"}, headers={"Idempotency-Key": key}
    )


async def settled(e):
    await asyncio.gather(*list(e.app["api"].events.tasks))


def test_headless_health_and_old_routes_removed(tmp_path):
    async def go():
        async with env(tmp_path, serve_ui=False, web_dir="/missing", up=False) as e:
            await e.backend.close()
            assert (await e.client.get("/api/v1/health")).status == 200
            state = await (await e.client.get("/api/v1/state")).json()
            validate("State", state)
            assert state["robot"] is None and not state["data_fresh"]
            for path in ("/", "/api/auth/status", "/ros/ws", "/api/op/tour/start"):
                response = await e.client.get(path)
                assert response.status == 404
                validate("Error", await response.json())

    run(go())


def test_explicit_route_idempotency_and_preferences_independent(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            await e.client.post(
                "/api/v1/reference/preferences",
                json={"tour_id": "expo_one", "confirm_between_stops": True},
            )
            response = await start(e)
            assert response.status == 202
            op = await response.json()
            validate("Operation", op)
            await settled(e)
            assert (await e.client.post("/api/v1/stack/restart")).status == 409
            again = await start(e)
            assert (await again.json())["id"] == op["id"]
            assert len(e.calls) == 1
            sent = e.calls[0]["body"]
            assert sent["tour_id"] == "expo_two" and sent["confirm_between_stops"] is False
            assert sent["greet"] and sent["narrate"] and sent["return_home"]
            assert sent["_operation_id"] == op["id"]
            assert e.calls[0]["headers"]["X-Bridge-Token"] == "tok"
            assert (await start(e, body={"tour_id": "different"})).status == 409
            assert (
                await start(e, key="bad", body={"tour_id": "x", "location_ids": ["y"]})
            ).status == 400
            assert len(e.calls) == 1

    run(go())


@pytest.mark.parametrize(
    "kind,body",
    [("missions", {"location_ids": ["hall"], "greet": False}), ("speech", {"text": "hello"})],
)
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "canceled", "rejected"])
def test_feedback_result_cancel_and_reconnect(tmp_path, kind, body, outcome):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            op = await (await start(e, kind=kind, body=body)).json()
            await settled(e)
            feedback = (
                {"phase": 1, "stop_index": 0, "stop_total": 1, "stop_id": "one", "eta_s": 0.0}
                if kind == "missions"
                else {
                    "clause_index": 0,
                    "clause_count": 1,
                    "progress": 0.5,
                    "current_clause": "hello",
                }
            )
            e.set_frame(operations=[{"id": op["id"], "status": "running", "feedback": feedback}])
            read = await (await e.client.get("/api/v1/operations/" + op["id"])).json()
            assert read["feedback"] == feedback
            e.set_frame(
                operations=[{"id": op["id"], "status": outcome, "result": {"detail": "done"}}]
            )
            read = await (await e.client.get("/api/v1/operations/" + op["id"])).json()
            assert read["status"] == outcome
            for _ in range(2):
                ws = await e.client.ws_connect("/api/v1/events")
                event = await ws.receive_json(timeout=2)
                jsonschema.validate(event, json.loads((SPEC / "events.schema.json").read_text()))
                assert event["type"] == "snapshot"
                assert event["data"]["operations"][0]["status"] == outcome
                assert "_owner" not in str(event)
                await ws.close()
            assert (
                await e.client.post("/api/v1/operations/" + op["id"] + "/cancel")
            ).status == 409

    run(go())


def test_unknown_after_timeout_or_connection_loss_no_resend(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            e.backend.app["statuses"]["/internal/missions"] = 504
            op = await (await start(e)).json()
            await settled(e)
            read = await (await e.client.get("/api/v1/operations/" + op["id"])).json()
            assert read["status"] == "unknown"
            e.watcher.frame = None
            assert (await start(e)).status == 202
            assert len(e.calls) == 1
            assert (await start(e, key="new")).status == 503
            e.set_frame()
            speech = await (
                await start(e, kind="speech", key="speech", body={"text": "test"})
            ).json()
            await settled(e)
            e.watcher.frame = None
            read = await (await e.client.get("/api/v1/operations/" + speech["id"])).json()
            assert read["status"] == "unknown"

    run(go())


def test_public_policy_and_restart_fail_closed(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            for frame in (
                None,
                {"state_name": "unknown"},
                {"state_name": "navigating"},
                {"mission_state_age_s": 4},
            ):
                e.set_frame(**(frame or {}))
                if frame is None:
                    e.watcher.frame = None
                assert (await e.client.post("/api/v1/stack/restart")).status == 409
            e.set_frame()
            assert (await _public(e, tour_id="expo_two")).status == 403
            response = await _public(e, tour_id="expo_one")
            assert response.status == 403
            response = await start(e)
            assert response.status == 202
            await settled(e)

    run(go())


def test_cors_error_shape_and_operation_owner(tmp_path):
    async def go():
        async with env(tmp_path, allowed_origins=("http://localhost:3000",)) as e:
            r = await e.client.get("/api/v1/health", headers={"Origin": "https://evil.test"})
            assert r.status == 403
            validate("Error", await r.json())
            r = await e.client.options(
                "/api/v1/missions", headers={"Origin": "http://localhost:3000"}
            )
            assert r.status == 204 and r.headers["Access-Control-Allow-Credentials"] == "true"
            await e.login()
            e.set_frame()
            op = await (await start(e)).json()
            await settled(e)
            await e.client.post("/api/v1/auth/logout")
            await e.login()
            assert (
                await e.client.post("/api/v1/operations/" + op["id"] + "/cancel")
            ).status == 403

    run(go())


def test_bounded_registry_does_not_evict_idempotency(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame()
            e.app["api"].events.limit = 1
            one = await (await start(e)).json()
            await settled(e)
            assert (await start(e, key="two")).status == 409
            assert (await (await start(e)).json())["id"] == one["id"]

    run(go())


def test_heartbeat_fanout_and_disconnect(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            task = asyncio.create_task(e.app["api"].events.run())
            try:
                one = await e.client.ws_connect("/api/v1/events")
                two = await e.client.ws_connect("/api/v1/events")
                first = await one.receive_json(timeout=2)
                assert (await two.receive_json(timeout=2))["type"] == "snapshot"
                next_event = await one.receive_json(timeout=2)
                assert next_event["seq"] > first["seq"]
                assert next_event["type"] == "heartbeat"
                await one.close()
                await two.receive_json(timeout=2)
                assert (await two.receive_json(timeout=2))["type"] == "heartbeat"
                await two.close()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(go())


def test_stopped_stack_requires_recent_positive_evidence(tmp_path):
    async def go():
        async with env(tmp_path, up=False) as e:
            api = e.app["api"]
            assert api.restart_guard() == "mission_state_stale"
            await e.backend.close()
            e.docker.running = False
            await e.monitor.poll()
            assert api.restart_guard() is None
            e.clock.t += 4
            assert api.restart_guard() == "mission_state_stale"

    run(go())


def test_localization_blocks_motion_and_survives_ws_reconnect(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            lost = dict(state="lost", reason="uncertainty_exceeded", motion_blocked=True,
                        reset_required=True, fresh=True, position_sigma=.7, yaw_sigma=.1)
            e.set_frame(localization=lost)
            response = await start(e)
            assert response.status == 409
            assert (await response.json())["error"]["code"] == "localization_required"
            assert not e.calls
            for _ in range(2):
                ws = await e.client.ws_connect("/api/v1/events")
                event = await ws.receive_json(timeout=2)
                assert event["data"]["robot"]["localization"] == lost
                jsonschema.validate(event, json.loads((SPEC / "events.schema.json").read_text()))
                await ws.close()
    run(go())


def test_stale_ready_localization_delegates_refresh_to_ros(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            await e.login()
            e.set_frame(localization=dict(state='ready', fresh=False,
                        motion_blocked=True, reset_required=False))
            response = await start(e)
            assert response.status == 202
            await settled(e)
            assert e.calls
    run(go())
