"""ROS adapters for the private API transport. Mission FSM remains authoritative."""

import asyncio
import queue

from rosidl_runtime_py.convert import message_to_ordereddict
from std_srvs.srv import Trigger

from guide_robot_msgs.action import GoToPose, RunTour, Say
from guide_robot_msgs.srv import (
    GetExhibitContent,
    ListLocations,
    ListTours,
    OverrideFace,
    Redirect,
    ResetLocalization,
    SubmitAnswer,
)


def plain(msg):
    """Convert a generated ROS message to JSON-compatible fields."""
    return dict(message_to_ordereddict(msg))


class CommandAdapter:
    """Adapt private transport commands to ROS without a second mission FSM."""

    def _drain_requests(self):
        while True:
            try:
                make, result = self._requests.get_nowait()
            except queue.Empty:
                return
            if result.cancelled():
                continue
            try:
                value = make()
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
            else:
                if not result.done():
                    result.set_result(value)

    def _on_face(self, msg):
        self._face_state = plain(msg)
        self._push_frame()

    def _on_speech(self, msg):
        self._speech_state = plain(msg)
        self._push_frame()

    def readiness(self):
        """Report discovery readiness independently for each subsystem."""
        return {
            "mission": self._run_tour_client.server_is_ready(),
            "pose": self._pose_client.server_is_ready(),
            "speech": self._say_client.server_is_ready(),
            "catalog": self._list_tours_client.service_is_ready(),
            "face": self._extra_clients["face"].service_is_ready(),
        }

    async def _publish_snapshot(self, frame):
        frame["operations"] = self._operations.snapshot()
        await self._server.push(frame)

    async def _service(self, client, request):
        if not client.service_is_ready():
            return 503, {"message": "subsystem_unavailable"}
        response = await self._call_ros(
            lambda: client.call_async(request), self._service_timeout_s
        )
        data = plain(response)
        accepted = data.get("accepted", data.get("success", True))
        return (200 if accepted else 409), data

    async def _action(self, kind, body):
        operation_id = body.pop("_operation_id")
        client = (
            self._pose_client
            if kind == "pose"
            else self._run_tour_client
            if kind == "mission"
            else self._say_client
        )
        if not client.server_is_ready():
            return 503, {"message": "subsystem_unavailable"}
        if kind == "pose":
            goal = GoToPose.Goal(
                map_version=body["map_version"], pose=self._map.pose(body, free=True)
            )
        else:
            goal = RunTour.Goal(**body) if kind == "mission" else Say.Goal(**body)
        if not self._operations.add(operation_id, kind):
            return 202, self._operations.items[operation_id]

        def feedback(msg):
            self._loop.call_soon_threadsafe(
                lambda: self._operations.update(operation_id, feedback=plain(msg.feedback))
            )

        try:
            handle = await self._call_ros(
                lambda: client.send_goal_async(goal, feedback_callback=feedback),
                self._service_timeout_s,
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._operations.update(operation_id, status="unknown")
            return 504, {"message": "outcome_unknown"}
        if not handle.accepted:
            self._operations.update(operation_id, status="rejected")
            return 409, self._operations.items[operation_id]
        self._operations.handles[operation_id] = handle
        self._operations.update(
            operation_id, status="running", goal_id=[int(value) for value in handle.goal_id.uuid]
        )

        def done(future):
            try:
                response = future.result()
                data = plain(response.result)
                status = {4: "succeeded", 5: "canceled", 6: "failed"}.get(
                    response.status, "unknown"
                )
                if kind == "speech" and data.get("status") not in (0,):
                    status = "canceled" if data.get("status") in (1, 2) else "failed"
                if kind in ("mission", "pose") and data.get("outcome") not in (0,):
                    status = "canceled" if data.get("outcome") == 1 else "failed"
            except Exception as exc:
                status, data = "unknown", {"message": str(exc)}
            self._loop.call_soon_threadsafe(
                lambda: self._operations.update(operation_id, status=status, result=data)
            )

        handle.get_result_async().add_done_callback(done)
        return 202, self._operations.items[operation_id]

    async def dispatch(self, method, resource, body):
        """Dispatch one trusted private request to an action, service or catalog."""
        if method == "GET":
            if resource == "map":
                return 200, self._map.state()
            if resource == "map/image":
                from aiohttp import web

                snapshot = self._map.current
                if snapshot is None:
                    return 503, {"message": "map_unavailable"}
                return 200, web.Response(
                    body=snapshot.png(),
                    content_type="image/png",
                    headers={"ETag": snapshot.version},
                )
            if resource == "localization/presets":
                allowed = list(self.get_parameter("localization_presets").value)
                found = {}
                for category in ("", "charging"):
                    status, data = await self._service(
                        self._list_locations_client, ListLocations.Request(category=category)
                    )
                    if status != 200:
                        return status, data
                    found.update({loc["id"]: loc for loc in data.get("locations", [])})
                return 200, {"presets": [found[key] for key in allowed if key in found]}
            if resource == "face/expressions":
                from pathlib import Path

                import yaml
                from ament_index_python.packages import get_package_share_directory

                path = (
                    Path(get_package_share_directory("guide_robot_face"))
                    / "config/face_states.yaml"
                )
                return 200, {"expressions": list(yaml.safe_load(path.read_text())["states"])}

            if resource == "tours":
                return await self._service(
                    self._list_tours_client, ListTours.Request(language=self._tours_language)
                )
            if resource == "locations":
                return await self._service(self._list_locations_client, ListLocations.Request())
            if resource.startswith("content/"):
                return await self._service(
                    self._get_exhibit_content_client,
                    GetExhibitContent.Request(
                        exhibit_id=resource[8:], language=self._content_language, mode="full"
                    ),
                )
            if resource.startswith("media/"):
                return await self._on_api_media(exhibit_id=resource[6:])
            if resource.startswith("operations/"):
                item = self._operations.items.get(resource.split("/")[1])
                return (200, item) if item else (404, {"message": "operation_not_found"})
        if method != "POST":
            return 404, {"message": "not_found"}
        if resource == "missions/go-to-pose":
            try:
                return await self._action("pose", dict(body))
            except ValueError as exc:
                return 409, {"message": str(exc)}
        if resource in ("missions", "speech"):
            return await self._action(
                "mission" if resource == "missions" else "speech", dict(body)
            )
        if resource.startswith("operations/") and resource.endswith("/cancel"):
            operation_id = resource.split("/")[1]
            handle = self._operations.handles.get(operation_id)
            if handle is None:
                return 409, {"message": "operation_not_cancelable"}
            response = await self._call_ros(handle.cancel_goal_async, self._service_timeout_s)
            return (202 if response.goals_canceling else 409), {
                "accepted": bool(response.goals_canceling)
            }
        triggers = {
            "missions/stop": self._request_stop_client,
            "missions/return": self._go_home_client,
            "missions/pause": self._extra_clients["pause"],
            "missions/resume": self._extra_clients["resume"],
            "interaction/question": self._request_question_client,
        }
        if resource == "missions/check-idle":
            return await self._service(self._extra_clients["check_idle"], Trigger.Request())
        if resource in triggers:
            return await self._service(triggers[resource], Trigger.Request())
        if resource == "missions/redirect":
            return await self._service(
                self._extra_clients["redirect"], Redirect.Request(location_id=body["location_id"])
            )
        if resource == "missions/base":
            return await self._on_api_go_to_base(location_id=body["location_id"])
        if resource == "interaction/confirm":
            return await self._on_api_confirm(answer=body["answer"])
        if resource == "interaction/answer":
            return await self._service(
                self._extra_clients["answer"],
                SubmitAnswer.Request(outcome={"resume": 0, "skip": 1, "end": 2}[body["outcome"]]),
            )
        if resource == "face/override":
            return await self._service(self._extra_clients["face"], OverrideFace.Request(**body))
        if resource == "localization/reset":
            if "pose" not in body:
                status, presets = await self.dispatch("GET", "localization/presets", {})
                if status != 200:
                    return status, presets
                candidates = presets["presets"]
                requested = body.get("preset_id")
                selected = (
                    next((p for p in candidates if p["id"] == requested), None)
                    if requested
                    else next(iter(candidates), None)
                )
                if selected is None or self._map.current is None:
                    return 409, {"message": "preset_or_map_missing"}
                from types import SimpleNamespace

                from guide_robot_api.lib.map_geometry import quaternion_yaw

                if selected["pose"]["header"]["frame_id"] != self._map.current.frame:
                    return 409, {"message": "preset_frame_mismatch"}
                position = selected["pose"]["pose"]["position"]
                orientation = selected["pose"]["pose"]["orientation"]
                body = {
                    "map_version": body.get("map_version", self._map.current.version),
                    "pose": {
                        "x": position["x"],
                        "y": position["y"],
                        "yaw": quaternion_yaw(SimpleNamespace(**orientation)),
                    },
                }
            try:
                pose = self._map.pose(body, free=False)
            except ValueError as exc:
                return 409, {"message": str(exc)}
            return await self._service(
                self._reset_pose_client,
                ResetLocalization.Request(map_version=body["map_version"], pose=pose),
            )
        if resource == "costmaps/clear":
            return await self._on_api_costmaps_clear()
        return 404, {"message": "not_found"}
