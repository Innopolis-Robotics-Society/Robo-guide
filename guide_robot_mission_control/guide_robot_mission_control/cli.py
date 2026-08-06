"""`ros2 run guide_robot_mission_control mission_cli ...` -- ручное управление и отладка.

design §11.

Каждая подкоманда -- отдельный короткий вызов: узел `mission_cli` поднимается,
делает один ROS-вызов (топик/сервис/action), печатает результат, гасится.
Без sim-времени и моков -- это инструмент для реального (или запущенного
вручную) стека, а не тест; синхронизация через `rclpy.spin_until_future_complete`
с реальным таймаутом, а не sim-clock идиома из test/.

`ask` (design §11: отправка `AskUser`-goal со свободным вопросом и
`option_phrases`) здесь **нет** -- `AskUser`-сервер в `mission_fsm` не
реализован, это осознанно отложено вместе с самим `AskUser` (см. докстринг
`fsm/states/awaiting_confirm.py`: подтверждение в v1 идёт через тестовый хук
`submit_confirm`, не через ASR/LLM-матчинг синонимов).

`pause`/`resume`/`confirm` дёргают тонкие ROS-сервисы `mission_fsm`
(`~/request_pause`, `~/request_resume`, `~/submit_confirm`) -- обёртки над
теми же хуками `FsmContext`, что использует test/ (design §5.4 п.6, "нет
реального ASR/LLM"). Это ровно то же самое разделение, что и `barge`:
синтетический сигнал для ручной отладки пути, у которого пока нет реального
источника (presence/ASR).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time

import rclpy
from guide_robot_msgs.action import Narrate, RunTour, Say
from guide_robot_msgs.msg import CancelAll, MissionState
from rclpy.action import ActionClient
from rclpy.client import Client
from rclpy.node import Node
from std_msgs.msg import Bool
from std_srvs.srv import SetBool, Trigger

from guide_robot_mission_control.lib.qos import QOS_CANCEL_ALL, QOS_MISSION_STATE

__all__ = ["main"]

_STATE_NAMES = {
    MissionState.STATE_IDLE: "IDLE",
    MissionState.STATE_GREETING: "GREETING",
    MissionState.STATE_NAVIGATING: "NAVIGATING",
    MissionState.STATE_NARRATING: "NARRATING",
    MissionState.STATE_ANSWERING: "ANSWERING",
    MissionState.STATE_AWAITING_CONFIRM: "AWAITING_CONFIRM",
    MissionState.STATE_PAUSED: "PAUSED",
    MissionState.STATE_HELD: "HELD",
    MissionState.STATE_RETURNING: "RETURNING",
}

_RUN_TOUR_OUTCOME_NAMES = {
    RunTour.Result.OUTCOME_COMPLETED: "COMPLETED",
    RunTour.Result.OUTCOME_CANCELED: "CANCELED",
    RunTour.Result.OUTCOME_ABORTED: "ABORTED",
    RunTour.Result.OUTCOME_NO_VISITOR: "NO_VISITOR",
}

_NARRATE_OUTCOME_NAMES = {
    Narrate.Result.OUTCOME_COMPLETED: "COMPLETED",
    Narrate.Result.OUTCOME_PAUSED: "PAUSED",
    Narrate.Result.OUTCOME_INTERRUPTED: "INTERRUPTED",
    Narrate.Result.OUTCOME_ABORTED: "ABORTED",
    Narrate.Result.OUTCOME_REJECTED: "REJECTED",
}

_CANCEL_SCOPES = {
    "all": CancelAll.SCOPE_ALL,
    "narration": CancelAll.SCOPE_NARRATION,
    "dialog": CancelAll.SCOPE_DIALOG,
    "safety": CancelAll.SCOPE_SAFETY,
}

_DEFAULT_CALL_TIMEOUT_S = 5.0


def _fail(message: str) -> None:
    print(f"ошибка: {message}", file=sys.stderr)
    sys.exit(1)


def _call_service(
    node: Node, client: Client, request: object, *, timeout_s: float = _DEFAULT_CALL_TIMEOUT_S
) -> object:
    """Вызвать сервис синхронно, реальными секундами; завершает процесс при неудаче."""
    if not client.wait_for_service(timeout_sec=timeout_s):
        _fail(f"сервис {client.srv_name} недоступен")
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout_s)
    if not future.done():
        _fail(f"сервис {client.srv_name} не ответил за {timeout_s} с")
    return future.result()


def _format_mission_state(msg: MissionState) -> str:
    state = _STATE_NAMES.get(msg.state, str(msg.state))
    parts = [
        f"state={state}",
        f"stop={msg.stop_index + 1}/{msg.stop_total}" if msg.stop_total else "stop=-",
        f"stop_id={msg.stop_id or '-'}",
        f"exhibit_id={msg.exhibit_id or '-'}",
        f"resume_available={msg.resume_available}",
        f"presence={msg.presence}",
    ]
    if msg.detail:
        parts.append(f"detail={msg.detail!r}")
    return " ".join(parts)


# -- подкоманды -----------------------------------------------------------


def cmd_tour(node: Node, args: argparse.Namespace) -> None:
    """RunTour -- полный тур либо override списком остановок (design §11)."""
    client = ActionClient(node, RunTour, "run_tour")
    if not client.wait_for_server(timeout_sec=args.timeout_s):
        _fail("run_tour action-сервер недоступен -- mission_fsm активна?")

    goal = RunTour.Goal(
        tour_id=args.tour or "",
        location_ids=args.locations.split(",") if args.locations else [],
        start_index=args.start_index,
        greet=not args.no_greet,
        narrate=not args.no_narrate,
        confirm_between_stops=not args.no_confirm,
        return_home=not args.no_return_home,
    )

    def _on_feedback(feedback_msg: object) -> None:
        fb: RunTour.Feedback = feedback_msg.feedback  # type: ignore[attr-defined]
        phase = _STATE_NAMES.get(fb.phase, str(fb.phase))
        print(f"[feedback] phase={phase} stop={fb.stop_index + 1}/{fb.stop_total} "
              f"stop_id={fb.stop_id or '-'}")

    send_future = client.send_goal_async(goal, feedback_callback=_on_feedback)
    rclpy.spin_until_future_complete(node, send_future, timeout_sec=args.timeout_s)
    if not send_future.done():
        _fail("run_tour goal не был принят вовремя")
    goal_handle = send_future.result()
    if not goal_handle.accepted:
        _fail("run_tour goal отклонён -- второй активный тур?")

    print("тур принят, жду завершения (Ctrl+C -- отменить)...")
    result_future = goal_handle.get_result_async()
    try:
        rclpy.spin_until_future_complete(node, result_future)
    except KeyboardInterrupt:
        print("отмена по Ctrl+C...")
        cancel_future = goal_handle.cancel_goal_async()
        rclpy.spin_until_future_complete(node, cancel_future, timeout_sec=args.timeout_s)
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=args.timeout_s)

    if not result_future.done():
        _fail("run_tour не вернул результат")
    result: RunTour.Result = result_future.result().result
    outcome = _RUN_TOUR_OUTCOME_NAMES.get(result.outcome, str(result.outcome))
    print(
        f"outcome={outcome} stops_completed={result.stops_completed} "
        f"stops_skipped={result.stops_skipped} detail={result.detail!r}"
    )


def cmd_status(node: Node, args: argparse.Namespace) -> None:
    """Печатает `/mission/state`; по умолчанию следит (design §11)."""
    seen = []

    def _on_state(msg: MissionState) -> None:
        seen.append(msg)
        print(_format_mission_state(msg))

    node.create_subscription(MissionState, "/mission/state", _on_state, QOS_MISSION_STATE)

    if args.once:
        deadline = time.monotonic() + args.timeout_s
        while not seen and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.2)
        if not seen:
            _fail("/mission/state молчит -- mission_fsm активна?")
        return

    with contextlib.suppress(KeyboardInterrupt):
        rclpy.spin(node)


def cmd_pause(node: Node, args: argparse.Namespace) -> None:
    """Мягкая пауза через `~/request_pause`, либо синтетический estop при `--hard`."""
    if args.hard:
        pub = node.create_publisher(Bool, "/supervisor/estop", 10)
        # Publisher должен успеть обнаружить подписку mission_fsm -- без
        # паузы первое сообщение рискует уйти до завершения discovery
        # (тот же эффект, что бил тесты step 7, см. test_safety_hold.py).
        rclpy.spin_once(node, timeout_sec=1.0)
        pub.publish(Bool(data=True))
        print("опубликован /supervisor/estop=true (синтетический safety-стоп)")
        return

    client = node.create_client(Trigger, "/mission_fsm/request_pause")
    response = _call_service(node, client, Trigger.Request(), timeout_s=args.timeout_s)
    if not response.success:
        _fail(response.message or "request_pause не сработал")
    print("пауза запрошена")


def cmd_resume(node: Node, args: argparse.Namespace) -> None:
    """Снимает синтетический estop (если был) и запрашивает возобновление из PAUSED."""
    pub = node.create_publisher(Bool, "/supervisor/estop", 10)
    rclpy.spin_once(node, timeout_sec=1.0)
    pub.publish(Bool(data=False))

    client = node.create_client(Trigger, "/mission_fsm/request_resume")
    response = _call_service(node, client, Trigger.Request(), timeout_s=args.timeout_s)
    print("опубликован /supervisor/estop=false; request_resume: " + (response.message or "ok"))


def cmd_confirm(node: Node, args: argparse.Namespace) -> None:
    """Ответ да/нет на «Идём дальше?» через `~/submit_confirm` (не в §11, но та же ROS-обвязка)."""
    client = node.create_client(SetBool, "/mission_fsm/submit_confirm")
    response = _call_service(
        node, client, SetBool.Request(data=args.answer == "yes"), timeout_s=args.timeout_s
    )
    if not response.success:
        _fail(response.message or "submit_confirm не сработал")
    print("ответ передан")


def cmd_say(node: Node, args: argparse.Namespace) -> None:
    """Прямой Narrate в narration_server, без тура (design §11)."""
    client = ActionClient(node, Narrate, "narrate")
    if not client.wait_for_server(timeout_sec=args.timeout_s):
        _fail("narrate action-сервер недоступен -- narration_server активен?")

    goal = Narrate.Goal(
        exhibit_id=args.exhibit_id,
        text=args.text,
        priority=Say.Goal.PRIORITY_NARRATION,
        scope=Say.Goal.SCOPE_NARRATION,
        continuity=Narrate.Goal.CONTINUITY_CONTINUOUS,
    )
    send_future = client.send_goal_async(goal)
    rclpy.spin_until_future_complete(node, send_future, timeout_sec=args.timeout_s)
    if not send_future.done():
        _fail("narrate goal не был принят вовремя")
    goal_handle = send_future.result()
    if not goal_handle.accepted:
        _fail("narrate goal отклонён")

    result_future = goal_handle.get_result_async()
    rclpy.spin_until_future_complete(node, result_future)
    result: Narrate.Result = result_future.result().result
    outcome = _NARRATE_OUTCOME_NAMES.get(result.outcome, str(result.outcome))
    print(f"outcome={outcome} spoken_text={result.spoken_text!r} detail={result.detail!r}")


def cmd_barge(node: Node, args: argparse.Namespace) -> None:
    """Синтетический barge-in на `/speech/cancel_all` -- отладка пути прерывания без VAD."""
    pub = node.create_publisher(CancelAll, "/speech/cancel_all", QOS_CANCEL_ALL)
    rclpy.spin_once(node, timeout_sec=1.0)
    msg = CancelAll(scope=_CANCEL_SCOPES[args.scope], reason=CancelAll.REASON_BARGE_IN)
    msg.stamp = node.get_clock().now().to_msg()
    pub.publish(msg)
    print(f"опубликован синтетический barge-in (scope={args.scope})")


# -- argparse ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mission_cli", description=__doc__)
    parser.add_argument(
        "--timeout-s", type=float, default=_DEFAULT_CALL_TIMEOUT_S, help="таймаут ROS-вызовов"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_tour = sub.add_parser("tour", help="запустить RunTour")
    p_tour.add_argument("--tour", default="", help="tour_id из ListTours")
    p_tour.add_argument("--locations", default="", help="через запятую -- override остановок")
    p_tour.add_argument("--start-index", type=int, default=0)
    p_tour.add_argument("--no-confirm", action="store_true")
    p_tour.add_argument("--no-greet", action="store_true")
    p_tour.add_argument("--no-narrate", action="store_true")
    p_tour.add_argument("--no-return-home", action="store_true")
    p_tour.set_defaults(func=cmd_tour)

    p_status = sub.add_parser("status", help="печатает MissionState, следит")
    p_status.add_argument("--once", action="store_true", help="напечатать одно сообщение и выйти")
    p_status.set_defaults(func=cmd_status)

    p_pause = sub.add_parser("pause", help="мягкая пауза (или --hard -- синтетический estop)")
    p_pause.add_argument("--hard", action="store_true")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="снять синтетический estop + возобновить из PAUSED")
    p_resume.set_defaults(func=cmd_resume)

    p_confirm = sub.add_parser("confirm", help="ответ на «Идём дальше?» (не из §11)")
    p_confirm.add_argument("answer", choices=["yes", "no"])
    p_confirm.set_defaults(func=cmd_confirm)

    p_say = sub.add_parser("say", help="прямой Narrate, без тура")
    p_say.add_argument("text")
    p_say.add_argument("--exhibit-id", default="cli-adhoc")
    p_say.set_defaults(func=cmd_say)

    p_barge = sub.add_parser("barge", help="синтетический barge-in для ручной отладки")
    p_barge.add_argument("--scope", choices=sorted(_CANCEL_SCOPES), default="narration")
    p_barge.set_defaults(func=cmd_barge)

    return parser


def main(args: list[str] | None = None) -> None:
    """Точка входа `mission_cli`."""
    parser = _build_parser()
    parsed = parser.parse_args(args)

    rclpy.init()
    node = Node("mission_cli")
    try:
        parsed.func(node, parsed)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
