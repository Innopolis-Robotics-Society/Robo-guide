"""face_node -- ROS-обвязка над FaceServer.

Держит aiohttp-сервер в отдельном потоке со своим event loop (rclpy
крутится синхронно в основном потоке), мост между ними --
``asyncio.run_coroutine_threadsafe``. Валидирует state из /face/state и
/face/set_state против набора состояний, реально загруженных из
face_states.yaml -- невалидированная строка в браузер не идёт (§3.1).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from rclpy.node import Node

from guide_robot_face.face_server import FaceServer
from guide_robot_face.lib.qos import QOS_FACE_STATE
from guide_robot_msgs.msg import FaceState
from guide_robot_msgs.srv import SetFaceState

_SERVER_START_TIMEOUT_S = 10.0
_SERVER_STOP_TIMEOUT_S = 5.0


class FaceNode(Node):
    """Публикатор /face/state -> websocket, сервис /face/set_state."""

    def __init__(self) -> None:
        """Загрузить face_states.yaml, поднять FaceServer, подписаться на /face/state."""
        super().__init__("face_node")

        share = Path(get_package_share_directory("guide_robot_face"))
        self.declare_parameter("http_host", "0.0.0.0")
        self.declare_parameter("http_port", 8090)
        self.declare_parameter("web_root", str(share / "web"))
        self.declare_parameter("states_yaml", str(share / "config" / "face_states.yaml"))

        http_host = str(self.get_parameter("http_host").value)
        http_port = int(self.get_parameter("http_port").value)
        web_root = Path(str(self.get_parameter("web_root").value))
        states_yaml_path = Path(str(self.get_parameter("states_yaml").value))

        with states_yaml_path.open(encoding="utf-8") as f:
            states_doc = yaml.safe_load(f)
        self._known_states: set[str] = set((states_doc or {}).get("states", {}).keys())
        if not self._known_states:
            raise RuntimeError(f"{states_yaml_path}: не содержит ни одного состояния")

        self._last_state = (
            "idle" if "idle" in self._known_states else next(iter(self._known_states))
        )
        self._last_gaze_az = 0.0
        self._last_seq = 0

        self._server = FaceServer(web_root=web_root, states=states_doc)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._server_thread = threading.Thread(
            target=self._run_server, args=(http_host, http_port), daemon=True
        )
        self._server_thread.start()
        if not self._loop_ready.wait(timeout=_SERVER_START_TIMEOUT_S):
            raise RuntimeError("face_server не поднялся за отведённое время")

        self.create_subscription(FaceState, "/face/state", self._on_face_state, QOS_FACE_STATE)
        self.create_service(SetFaceState, "/face/set_state", self._on_set_state)

        self.get_logger().info(f"face_node: http://{http_host}:{http_port}, web_root={web_root}")

    def _run_server(self, host: str, port: int) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._server.start(host, port))
        self._loop_ready.set()
        loop.run_forever()

    def _validate(self, state: str) -> bool:
        if state not in self._known_states:
            self.get_logger().warning(
                f"неизвестное состояние лица '{state}', держим '{self._last_state}'",
                throttle_duration_sec=5.0,
            )
            return False
        return True

    def _apply(self, state: str, gaze_az: float, seq: int) -> None:
        self._last_state = state
        self._last_gaze_az = gaze_az
        self._last_seq = seq
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._server.push(state, gaze_az, seq), self._loop)

    def _on_face_state(self, msg: FaceState) -> None:
        if not self._validate(msg.state):
            return
        self._apply(msg.state, float(msg.gaze_az), int(msg.seq))

    def _on_set_state(
        self, request: SetFaceState.Request, response: SetFaceState.Response
    ) -> SetFaceState.Response:
        if not self._validate(request.state):
            response.accepted = False
            response.reason = f"unknown state: {request.state}"
            return response
        self._apply(request.state, float(request.gaze_az), int(request.seq))
        response.accepted = True
        response.reason = ""
        return response

    def destroy_node(self) -> None:
        """Остановить FaceServer и его поток перед уничтожением ноды."""
        if self._loop is not None:
            stop_fut = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
            try:
                stop_fut.result(timeout=_SERVER_STOP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 -- не мешать остановке ноды
                self.get_logger().warning("face_server: ошибка при остановке", exc_info=True)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._server_thread.join(timeout=_SERVER_STOP_TIMEOUT_S)
        super().destroy_node()


def main(args: list[str] | None = None) -> None:
    """Точка входа console_script face_node."""
    rclpy.init(args=args)
    node = FaceNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
