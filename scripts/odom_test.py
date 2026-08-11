#!/usr/bin/env python3
"""Запись и доказательный анализ напольных тестов колесной одометрии."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = ROOT / "odom_test_runs"
ROBOT_PARAMS = ROOT / "guide_robot_description/config/robot_params.yaml"
TOPICS = [
    "/joint_states",
    "/odom",
    "/cmd_vel_nav",
    "/cmd_vel",
    "/diff_drive_controller/cmd_vel_unstamped",
    "/tf",
    "/tf_static",
    "/rosout",
    "/parameter_events",
]
SPEED_PROFILE = (
    ("вперед 0.10 м/с", 0.10, 3.0),
    ("стоп", 0.0, 2.0),
    ("назад 0.10 м/с", -0.10, 3.0),
    ("стоп", 0.0, 2.0),
    ("вперед 0.20 м/с", 0.20, 3.0),
    ("стоп", 0.0, 2.0),
    ("назад 0.20 м/с", -0.20, 3.0),
    ("стоп", 0.0, 2.0),
    ("вперед 0.35 м/с", 0.35, 3.0),
    ("стоп", 0.0, 2.0),
    ("назад 0.35 м/с", -0.35, 3.0),
    ("стоп", 0.0, 2.0),
    ("вперед 0.50 м/с", 0.50, 3.0),
    ("стоп", 0.0, 2.0),
    ("назад 0.50 м/с", -0.50, 3.0),
    ("стоп", 0.0, 2.0),
    ("вперед 0.60 м/с", 0.60, 3.0),
    ("стоп", 0.0, 2.0),
    ("назад 0.60 м/с", -0.60, 3.0),
    ("финальный стоп", 0.0, 3.0),
)


@dataclass
class Sample:
    """Один временной отсчет."""

    time: float
    values: tuple[float, ...]


def median(values: Sequence[float]) -> float | None:
    """Вернуть медиану или None для пустой последовательности."""
    return statistics.median(values) if values else None


def within(value: float, target: float, tolerance: float) -> bool:
    """Сравнить с допуском без ложного FAIL на границе из-за float."""
    return abs(value - target) <= tolerance + 1e-12


def angle_delta(yaws: Sequence[float]) -> float:
    """Развернуть последовательность углов и вернуть полное изменение."""
    if len(yaws) < 2:
        return 0.0
    total = 0.0
    previous = yaws[0]
    for yaw in yaws[1:]:
        step = yaw - previous
        total += (step + math.pi) % (2.0 * math.pi) - math.pi
        previous = yaw
    return total


def quaternion_yaw(q: Any) -> float:
    """Получить yaw из geometry_msgs/Quaternion."""
    return math.atan2(
        2.0 * (q.w * q.z + q.x * q.y),
        1.0 - 2.0 * (q.y * q.y + q.z * q.z),
    )


def load_params(path: Path) -> dict[str, Any]:
    """Загрузить снимок единого файла физических параметров."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Нужен python3-yaml (входит в ROS 2 Humble)") from exc
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def command_output(command: list[str]) -> str:
    """Выполнить диагностическую команду, не прерывая запись при ошибке."""
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        return f"ERROR: {exc}\n"


def profile_peak_displacement(profile: Sequence[tuple[str, float, float]]) -> tuple[float, float]:
    """Вернуть максимальное удаление и итоговую командную координату."""
    position = 0.0
    peak = 0.0
    for _, speed, duration in profile:
        position += speed * duration
        peak = max(peak, abs(position))
    return peak, position


def prepare_run(
    name: str,
    kind: str,
    output_dir: Path,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Создать каталог прогона и сохранить provenance."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_dir.resolve() / f"{stamp}_{name}"
    bag_dir = run_dir / "bag"
    run_dir.mkdir(parents=True)

    metadata = {
        "schema": 1,
        "name": name,
        "kind": kind,
        "created_utc": stamp,
        "git_commit": command_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip(),
        "topics": TOPICS,
    }
    metadata.update(extra_metadata or {})
    (run_dir / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copy2(ROBOT_PARAMS, run_dir / "robot_params.yaml")
    (run_dir / "controller_params.yaml").write_text(
        command_output(["ros2", "param", "dump", "/diff_drive_controller"]),
        encoding="utf-8",
    )
    (run_dir / "hardware_interfaces.txt").write_text(
        command_output(["ros2", "control", "list_hardware_interfaces"]),
        encoding="utf-8",
    )
    (run_dir / "topics.txt").write_text(
        command_output(["ros2", "topic", "list", "-t"]),
        encoding="utf-8",
    )
    return run_dir, bag_dir


def stop_bag(process: subprocess.Popen[Any]) -> int:
    """Корректно завершить rosbag, чтобы metadata.yaml успел записаться."""
    if process.poll() is not None:
        return process.returncode
    process.send_signal(signal.SIGINT)
    try:
        return process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
        return process.wait()


def record(args: argparse.Namespace) -> int:
    """Сохранить bag и provenance, не посылая роботу команд движения."""
    run_dir, bag_dir = prepare_run(args.name, args.kind, args.output_dir)

    command = ["ros2", "bag", "record", "-o", str(bag_dir), *TOPICS]
    print(f"Каталог прогона: {run_dir}")
    print("Запись началась. Движение этот скрипт НЕ запускает. Ctrl-C завершит bag.")
    process = subprocess.Popen(command)
    try:
        return process.wait()
    except KeyboardInterrupt:
        return stop_bag(process)


def require_active_collision_monitor() -> None:
    """Не разрешать автоматическое движение без последнего safety-слоя."""
    result = subprocess.run(
        ["ros2", "lifecycle", "get", "/collision_monitor"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip().lower().startswith("active"):
        state = result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"collision_monitor не active: {state}")


def publish_twist(publisher: Any, node: Any, speed: float, duration: float) -> None:
    """Публиковать одну ступень профиля с частотой 20 Гц."""
    import rclpy
    from geometry_msgs.msg import Twist

    message = Twist()
    message.linear.x = speed
    deadline = time.monotonic() + duration
    next_tick = time.monotonic()
    while time.monotonic() < deadline:
        publisher.publish(message)
        rclpy.spin_once(node, timeout_sec=0.0)
        next_tick += 0.05
        time.sleep(max(0.0, next_tick - time.monotonic()))


def speed_test(args: argparse.Namespace) -> int:
    """Записать один автоматический out-and-back профиль скорости."""
    try:
        import rclpy
        from geometry_msgs.msg import Twist
    except ImportError as exc:
        raise RuntimeError("Нужно окружение ROS 2 Humble с rclpy") from exc

    require_active_collision_monitor()
    peak, finish = profile_peak_displacement(SPEED_PROFILE)
    print(
        f"Робот проедет до {peak:.2f} м вперед и автоматически вернется "
        f"(командный остаток {finish:.3f} м)."
    )
    print("Нужно 2.5 м свободного пола, оператор у физического аварийного стопа.")
    if input("Для старта напечатайте ЕДЕМ: ").strip() != "ЕДЕМ":
        print("Отменено, робот не двигался.")
        return 1

    run_dir, bag_dir = prepare_run(
        args.name,
        "speed",
        args.output_dir,
        {"speed_profile": SPEED_PROFILE},
    )
    bag = subprocess.Popen(["ros2", "bag", "record", "-o", str(bag_dir), *TOPICS])
    time.sleep(2.0)
    if bag.poll() is not None:
        raise RuntimeError(f"ros2 bag record завершился с кодом {bag.returncode}")

    rclpy.init()
    node = rclpy.create_node("guide_robot_odom_speed_test")
    publisher = node.create_publisher(Twist, "/cmd_vel_nav", 10)
    try:
        deadline = time.monotonic() + 5.0
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if publisher.get_subscription_count() == 0:
            raise RuntimeError("у /cmd_vel_nav нет подписчиков; движение не начато")

        publish_twist(publisher, node, 0.0, 2.0)
        for label, speed, duration in SPEED_PROFILE:
            print(f"{label}: {duration:.0f} с")
            publish_twist(publisher, node, speed, duration)
        print("Профиль завершен, робот остановлен.")
        return 0
    finally:
        # Даже при Ctrl-C/исключении активно отправляем ноль дольше одного
        # периода cmd_vel_timeout; watchdog hardware остается вторым рубежом.
        publish_twist(publisher, node, 0.0, 1.0)
        node.destroy_node()
        rclpy.shutdown()
        stop_bag(bag)
        print(f"Bag: {run_dir}")


def resolve_bag(path: Path) -> tuple[Path, Path]:
    """Вернуть каталог rosbag и каталог прогона."""
    path = path.resolve()
    if (path / "metadata.yaml").exists():
        return path, path.parent
    if (path / "bag/metadata.yaml").exists():
        return path / "bag", path
    raise FileNotFoundError(f"Не найден metadata.yaml в {path} или {path / 'bag'}")


def storage_identifier(bag_dir: Path) -> str:
    """Прочитать storage id без зависимости анализатора от PyYAML."""
    text = (bag_dir / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*storage_identifier:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else "sqlite3"


def read_bag(path: Path) -> dict[str, Any]:
    """Прочитать необходимые сообщения через штатный rosbag2_py Humble."""
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise RuntimeError("Нужно выполнять в окружении ROS 2 Humble с rosbag2_py") from exc

    bag_dir, run_dir = resolve_bag(path)
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(
            uri=str(bag_dir),
            storage_id=storage_identifier(bag_dir),
        ),
        rosbag2_py.ConverterOptions(
            input_serialization_format="",
            output_serialization_format="",
        ),
    )
    types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    wanted = {
        topic: get_message(type_name) for topic, type_name in types.items() if topic in TOPICS
    }
    joints: list[Sample] = []
    odom: list[Sample] = []
    commands: dict[str, list[Sample]] = {
        "/cmd_vel_nav": [],
        "/cmd_vel": [],
        "controller": [],
    }
    logs: list[str] = []

    while reader.has_next():
        topic, raw, timestamp = reader.read_next()
        if topic not in wanted:
            continue
        msg = deserialize_message(raw, wanted[topic])
        time_s = timestamp / 1e9
        if topic == "/joint_states":
            names = list(msg.name)
            if "left_wheel_joint" in names and "right_wheel_joint" in names:
                left = msg.position[names.index("left_wheel_joint")]
                right = msg.position[names.index("right_wheel_joint")]
                joints.append(Sample(time_s, (left, right)))
        elif topic == "/odom":
            pose = msg.pose.pose
            odom.append(
                Sample(
                    time_s,
                    (
                        pose.position.x,
                        pose.position.y,
                        quaternion_yaw(pose.orientation),
                        msg.twist.twist.linear.x,
                        msg.twist.twist.angular.z,
                    ),
                )
            )
        elif topic in (
            "/cmd_vel_nav",
            "/cmd_vel",
            "/diff_drive_controller/cmd_vel_unstamped",
        ):
            twist = msg.twist if hasattr(msg, "twist") else msg
            key = "controller" if topic.endswith("cmd_vel_unstamped") else topic
            commands[key].append(Sample(time_s, (twist.linear.x, twist.angular.z)))
        elif topic == "/rosout":
            logs.append(msg.msg)

    if not joints:
        raise RuntimeError("В bag нет /joint_states с обоими колесами")
    if not odom:
        raise RuntimeError("В bag нет /odom")
    return {
        "bag": str(bag_dir),
        "run_dir": run_dir,
        "joints": joints,
        "odom": odom,
        "commands": commands,
        "logs": logs,
    }


def run_params(data: dict[str, Any], override: Path | None) -> dict[str, Any]:
    """Найти параметры, с которыми был записан прогон."""
    run_dir = data["run_dir"]
    snapshot = run_dir / "robot_params.yaml"
    return load_params(override or (snapshot if snapshot.exists() else ROBOT_PARAMS))


def encoder_integrity(joints: Sequence[Sample], logs: Sequence[str]) -> dict[str, Any]:
    """Проверить реальную каденцию кадров, скачки и диагностические сообщения."""
    changed: list[tuple[Sample, tuple[int, int]]] = []
    for previous, current in zip(joints, joints[1:], strict=False):
        dl = current.values[0] - previous.values[0]
        dr = current.values[1] - previous.values[1]
        if abs(dl) > 1e-9 or abs(dr) > 1e-9:
            signs = (
                1 if dl > 1e-9 else -1 if dl < -1e-9 else 0,
                1 if dr > 1e-9 else -1 if dr < -1e-9 else 0,
            )
            changed.append((current, signs))
    gaps: list[float] = []
    rates: list[float] = []
    for (previous, previous_signs), (current, current_signs) in zip(
        changed, changed[1:], strict=False
    ):
        # Пауза между forward/reverse не является потерей кадров. Внутри одного
        # направления каждое изменение позиции соответствует новому кадру FURO.
        if previous_signs != current_signs:
            continue
        dt = current.time - previous.time
        if dt <= 0.0:
            continue
        gaps.append(dt)
        rates.extend(
            [
                abs(current.values[0] - previous.values[0]) / dt,
                abs(current.values[1] - previous.values[1]) / dt,
            ]
        )
    encoder_logs = [line for line in logs if "[encoders]" in line]
    hard_errors = [
        line
        for line in encoder_logs
        if any(word in line for word in ("невозможна", "отвергнут", "нет валидных"))
    ]
    repaired = [line for line in encoder_logs if "починен" in line]
    max_gap = max(gaps, default=None)
    return {
        "changed_samples": len(changed),
        "median_update_period_s": median(gaps),
        "max_update_gap_s": max_gap,
        "max_observed_wheel_rate_rad_s": max(rates, default=None),
        "repaired_wraps_in_log": len(repaired),
        "hard_encoder_errors": hard_errors,
        "pass": max_gap is not None and max_gap <= 0.5 and not hard_errors,
    }


def endpoint_delta(data: dict[str, Any]) -> dict[str, float]:
    """Посчитать изменения суставов и позы между краями bag."""
    joints = data["joints"]
    odom = data["odom"]
    left = joints[-1].values[0] - joints[0].values[0]
    right = joints[-1].values[1] - joints[0].values[1]
    x0, y0, yaw0 = odom[0].values[:3]
    x1, y1 = odom[-1].values[:2]
    dx = x1 - x0
    dy = y1 - y0
    return {
        "left_joint_rad": left,
        "right_joint_rad": right,
        "odom_forward_m": math.cos(yaw0) * dx + math.sin(yaw0) * dy,
        "odom_lateral_m": -math.sin(yaw0) * dx + math.cos(yaw0) * dy,
        "odom_yaw_rad": angle_delta([sample.values[2] for sample in odom]),
    }


def steady_speed(
    joints: Sequence[Sample],
    commands: dict[str, list[Sample]],
    radius_left: float,
    radius_right: float,
    scale_factor: float,
) -> tuple[float | None, float | None]:
    """Оценить скорость в центральной половине движения и команду на контроллере."""
    changed = [
        sample
        for index, sample in enumerate(joints)
        if index == 0 or sample.values != joints[index - 1].values
    ]
    if len(changed) < 4:
        return None, None
    start = changed[len(changed) // 4]
    stop = changed[(len(changed) * 3) // 4]
    dt = stop.time - start.time
    if dt <= 0.0:
        return None, None
    dl = (stop.values[0] - start.values[0]) * radius_left
    dr = (stop.values[1] - start.values[1]) * radius_right
    measured = ((dl + dr) / (2.0 * dt)) * scale_factor
    stream = commands["controller"] or commands["/cmd_vel"]
    linear = [
        sample.values[0]
        for sample in stream
        if start.time <= sample.time <= stop.time and abs(sample.values[0]) > 0.01
    ]
    return measured, median(linear)


def write_result(data: dict[str, Any], result: dict[str, Any], output: Path | None) -> None:
    """Записать машинно-читаемый результат рядом с прогоном."""
    destination = output or data["run_dir"] / "result.json"
    destination.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Результат: {destination}")


def analyze_straight(args: argparse.Namespace) -> int:
    """Проверить линейный масштаб, симметрию и командный коэффициент."""
    if args.distance <= 0.0:
        raise ValueError("--distance должна быть > 0")
    data = read_bag(args.bag)
    params = run_params(data, args.params)
    geometry = params["geometry"]
    odometry = params["odometry"]
    drive = params["drive"]
    delta = endpoint_delta(data)
    sign = 1.0 if args.direction == "forward" else -1.0
    actual = sign * args.distance
    radius_left = geometry["wheel_radius"] * odometry["left_wheel_radius_multiplier"]
    radius_right = geometry["wheel_radius"] * odometry["right_wheel_radius_multiplier"]
    left_m = delta["left_joint_rad"] * radius_left
    right_m = delta["right_joint_rad"] * radius_right
    encoder_m = (left_m + right_m) / 2.0
    scale_ratio = abs(encoder_m) / args.distance
    scale_factor = 1.0 / scale_ratio if scale_ratio else math.inf
    speed, command = steady_speed(
        data["joints"],
        data["commands"],
        radius_left,
        radius_right,
        scale_factor,
    )
    speed_ratio = abs(speed / command) if speed is not None and command else None
    speed_offset = drive.get("speed_offset", 0.0)
    coefficient = None
    if speed is not None and command and abs(speed) > speed_offset and abs(command) > speed_offset:
        coefficient = drive["speed_coefficient"] * (
            (abs(speed) - speed_offset) / (abs(command) - speed_offset)
        )
    expected_joint_sign = (
        delta["left_joint_rad"] * sign > 0.0 and delta["right_joint_rad"] * sign > 0.0
    )
    odom_sign = delta["odom_forward_m"] * sign > 0.0
    integrity = encoder_integrity(data["joints"], data["logs"])
    result = {
        "schema": 1,
        "kind": "straight",
        "bag": data["bag"],
        "direction": args.direction,
        "actual_distance_m": actual,
        **delta,
        "left_encoder_distance_m": left_m,
        "right_encoder_distance_m": right_m,
        "encoder_distance_m": encoder_m,
        "scale_ratio_reported_over_actual": scale_ratio,
        "scale_error_percent": (scale_ratio - 1.0) * 100.0,
        "suggested_wheel_radius_m": geometry["wheel_radius"] * scale_factor,
        "suggested_ticks_per_rev": drive["ticks_per_rev"] / scale_factor,
        "left_right_difference_percent": (
            (abs(right_m) - abs(left_m)) / ((abs(right_m) + abs(left_m)) / 2.0) * 100.0
            if left_m or right_m
            else math.inf
        ),
        "steady_measured_speed_m_s": speed,
        "steady_controller_command_m_s": command,
        "speed_tracking_ratio": speed_ratio,
        "configured_speed_coefficient": drive["speed_coefficient"],
        "configured_speed_offset": speed_offset,
        "estimated_speed_coefficient": coefficient,
        "signs_pass": expected_joint_sign and odom_sign,
        "integrity": integrity,
    }
    scale_pass = within(scale_ratio, 1.0, 0.03)
    speed_pass = speed_ratio is not None and within(speed_ratio, 1.0, 0.10)
    result["pass"] = scale_pass and speed_pass and result["signs_pass"] and integrity["pass"]
    result["status"] = "PASS" if result["pass"] else "FAIL"
    write_result(data, result, args.output)
    return 0 if result["pass"] else 2


def analyze_spin(args: argparse.Namespace) -> int:
    """Проверить эффективную колею по нескольким полным оборотам."""
    if args.turns <= 0.0:
        raise ValueError("--turns должно быть > 0")
    data = read_bag(args.bag)
    params = run_params(data, args.params)
    geometry = params["geometry"]
    odometry = params["odometry"]
    delta = endpoint_delta(data)
    sign = 1.0 if args.direction == "ccw" else -1.0
    actual_yaw = sign * args.turns * 2.0 * math.pi
    left_radius = geometry["wheel_radius"] * odometry["left_wheel_radius_multiplier"]
    right_radius = geometry["wheel_radius"] * odometry["right_wheel_radius_multiplier"]
    separation = geometry["wheel_separation"] * odometry["wheel_separation_multiplier"]
    joint_yaw = (
        delta["right_joint_rad"] * right_radius - delta["left_joint_rad"] * left_radius
    ) / separation
    yaw_ratio = abs(joint_yaw / actual_yaw)
    signs_pass = (
        delta["left_joint_rad"] * sign < 0.0
        and delta["right_joint_rad"] * sign > 0.0
        and delta["odom_yaw_rad"] * sign > 0.0
    )
    integrity = encoder_integrity(data["joints"], data["logs"])
    result = {
        "schema": 1,
        "kind": "spin",
        "bag": data["bag"],
        "direction": args.direction,
        "physical_turns": args.turns,
        "actual_yaw_rad": actual_yaw,
        **delta,
        "joint_yaw_rad": joint_yaw,
        "yaw_ratio_reported_over_actual": yaw_ratio,
        "yaw_error_percent": (yaw_ratio - 1.0) * 100.0,
        "configured_wheel_separation_multiplier": odometry["wheel_separation_multiplier"],
        "suggested_wheel_separation_multiplier": (
            odometry["wheel_separation_multiplier"] * yaw_ratio
        ),
        "odom_vs_joint_yaw_percent": (
            (abs(delta["odom_yaw_rad"] / joint_yaw) - 1.0) * 100.0 if joint_yaw else math.inf
        ),
        "signs_pass": signs_pass,
        "integrity": integrity,
    }
    result["pass"] = within(yaw_ratio, 1.0, 0.02) and signs_pass and integrity["pass"]
    result["status"] = "PASS" if result["pass"] else "FAIL"
    write_result(data, result, args.output)
    return 0 if result["pass"] else 2


def analyze_return(args: argparse.Namespace) -> int:
    """Проверить замыкание пути вперед-назад и механический гистерезис."""
    if args.physical_offset < 0.0:
        raise ValueError("--physical-offset должна быть >= 0")
    data = read_bag(args.bag)
    params = run_params(data, args.params)
    geometry = params["geometry"]
    odometry = params["odometry"]
    delta = endpoint_delta(data)
    left_radius = geometry["wheel_radius"] * odometry["left_wheel_radius_multiplier"]
    right_radius = geometry["wheel_radius"] * odometry["right_wheel_radius_multiplier"]
    left_closure = delta["left_joint_rad"] * left_radius
    right_closure = delta["right_joint_rad"] * right_radius
    encoder_closure = (left_closure + right_closure) / 2.0
    odom_closure = math.hypot(delta["odom_forward_m"], delta["odom_lateral_m"])
    integrity = encoder_integrity(data["joints"], data["logs"])
    result = {
        "schema": 1,
        "kind": "return",
        "bag": data["bag"],
        "physical_endpoint_offset_m": args.physical_offset,
        **delta,
        "left_encoder_closure_m": left_closure,
        "right_encoder_closure_m": right_closure,
        "encoder_closure_m": encoder_closure,
        "odom_closure_m": odom_closure,
        "integrity": integrity,
    }
    result["pass"] = (
        args.physical_offset <= 0.03
        and abs(encoder_closure) <= 0.03
        and odom_closure <= 0.03
        and abs(delta["odom_yaw_rad"]) <= 0.02
        and integrity["pass"]
    )
    result["status"] = "PASS" if result["pass"] else "FAIL"
    write_result(data, result, args.output)
    return 0 if result["pass"] else 2


def odom_sessions(samples: Sequence[Sample], gap: float = 0.75) -> list[list[Sample]]:
    """Разбить odom на сессии по паузе перезапуска."""
    sessions: list[list[Sample]] = [[samples[0]]]
    for previous, current in zip(samples, samples[1:], strict=False):
        if current.time - previous.time > gap:
            sessions.append([])
        sessions[-1].append(current)
    return sessions


def analyze_restart(args: argparse.Namespace) -> int:
    """Проверить нулевой старт odom после перезапуска без power cycle."""
    data = read_bag(args.bag)
    sessions = odom_sessions(data["odom"])
    latest = sessions[-1]
    first = latest[0].values
    first_second = [sample for sample in latest if sample.time - latest[0].time <= 1.0]
    max_translation = max(
        math.hypot(sample.values[0], sample.values[1]) for sample in first_second
    )
    max_yaw = max(abs(angle_delta([first[2], sample.values[2]])) for sample in first_second)
    result = {
        "schema": 1,
        "kind": "restart",
        "bag": data["bag"],
        "odom_sessions": len(sessions),
        "post_restart_initial_x_m": first[0],
        "post_restart_initial_y_m": first[1],
        "post_restart_initial_yaw_rad": first[2],
        "first_second_max_translation_m": max_translation,
        "first_second_max_yaw_rad": max_yaw,
    }
    result["pass"] = (
        len(sessions) >= 2
        and math.hypot(first[0], first[1]) <= 0.01
        and abs(first[2]) <= 0.01
        and max_translation <= 0.01
        and max_yaw <= 0.01
    )
    result["status"] = "PASS" if result["pass"] else "FAIL"
    write_result(data, result, args.output)
    return 0 if result["pass"] else 2


def result_files(paths: Sequence[Path]) -> list[Path]:
    """Развернуть аргументы отчета в список result.json."""
    files: list[Path] = []
    for path in paths:
        path = path.resolve()
        if path.is_file():
            files.append(path)
        else:
            files.extend(path.glob("**/result.json"))
    return sorted(set(files))


def report(args: argparse.Namespace) -> int:
    """Объединить прогоны и применить групповые критерии приемки."""
    files = result_files(args.paths)
    if not files:
        raise FileNotFoundError("Не найдено ни одного result.json")
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    straight = [run for run in runs if run["kind"] == "straight"]
    spins = [run for run in runs if run["kind"] == "spin"]

    scale_ratios = [run["scale_ratio_reported_over_actual"] for run in straight]
    speed_coefficients = [
        run["estimated_speed_coefficient"]
        for run in straight
        if run.get("estimated_speed_coefficient") is not None
    ]
    yaw_ratios = [run["yaw_ratio_reported_over_actual"] for run in spins]

    def direction_mean(items: list[dict[str, Any]], direction: str, field: str) -> float | None:
        return median([item[field] for item in items if item["direction"] == direction])

    forward_scale = direction_mean(straight, "forward", "scale_ratio_reported_over_actual")
    reverse_scale = direction_mean(straight, "reverse", "scale_ratio_reported_over_actual")
    ccw_yaw = direction_mean(spins, "ccw", "yaw_ratio_reported_over_actual")
    cw_yaw = direction_mean(spins, "cw", "yaw_ratio_reported_over_actual")
    scale_mean = statistics.mean(scale_ratios) if scale_ratios else None
    yaw_mean = statistics.mean(yaw_ratios) if yaw_ratios else None
    coefficient_mean = statistics.mean(speed_coefficients) if speed_coefficients else None
    coefficient_spread = (
        (max(speed_coefficients) - min(speed_coefficients)) / coefficient_mean
        if coefficient_mean and len(speed_coefficients) > 1
        else 0.0
    )

    checks = {
        "all_individual_runs_pass": all(run.get("pass", False) for run in runs),
        "straight_mean_within_2_percent": (
            scale_mean is not None and within(scale_mean, 1.0, 0.02)
        ),
        "straight_each_within_3_percent": (
            bool(scale_ratios) and all(within(value, 1.0, 0.03) for value in scale_ratios)
        ),
        "forward_reverse_within_2_percent": (
            forward_scale is not None
            and reverse_scale is not None
            and within(forward_scale, reverse_scale, 0.02)
        ),
        "spin_mean_within_2_percent": (yaw_mean is not None and within(yaw_mean, 1.0, 0.02)),
        "cw_ccw_within_2_percent": (
            cw_yaw is not None and ccw_yaw is not None and within(cw_yaw, ccw_yaw, 0.02)
        ),
        "speed_coefficient_each_within_10_percent": (
            bool(straight)
            and all(
                run.get("speed_tracking_ratio") is not None
                and within(run["speed_tracking_ratio"], 1.0, 0.10)
                for run in straight
            )
        ),
    }
    summary = {
        "schema": 1,
        "status": "PASS" if all(checks.values()) else "FAIL",
        "runs": len(runs),
        "straight_runs": len(straight),
        "spin_runs": len(spins),
        "scale_mean_ratio": scale_mean,
        "forward_scale_median": forward_scale,
        "reverse_scale_median": reverse_scale,
        "yaw_mean_ratio": yaw_mean,
        "cw_yaw_median": cw_yaw,
        "ccw_yaw_median": ccw_yaw,
        "estimated_speed_coefficient_mean": coefficient_mean,
        "speed_coefficient_spread_percent": coefficient_spread * 100.0,
        "speed_coefficient_nonlinearity_warning": coefficient_spread > 0.05,
        "checks": checks,
        "result_files": [str(path) for path in files],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "report.json"
    csv_path = args.output / "report.csv"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["kind", "direction", "status", "scale_ratio", "yaw_ratio", "speed_coefficient"]
        )
        for run in runs:
            writer.writerow(
                [
                    run["kind"],
                    run.get("direction", ""),
                    run["status"],
                    run.get("scale_ratio_reported_over_actual", ""),
                    run.get("yaw_ratio_reported_over_actual", ""),
                    run.get("estimated_speed_coefficient", ""),
                ]
            )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Отчет: {json_path}\nCSV: {csv_path}")
    return 0 if summary["status"] == "PASS" else 2


def parser() -> argparse.ArgumentParser:
    """Построить CLI."""
    main = argparse.ArgumentParser(
        description="Запись и анализ напольных тестов колесной одометрии Guide-Robot."
    )
    commands = main.add_subparsers(dest="command", required=True)

    speed = commands.add_parser(
        "speed-test",
        help="одним запуском записать автоматический профиль вперед-назад",
    )
    speed.add_argument("--name", default="speed_auto", help="имя каталога прогона")
    speed.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS)
    speed.set_defaults(func=speed_test)

    record_parser = commands.add_parser("record", help="записать один прогон")
    record_parser.add_argument("name", help="короткое имя прогона")
    record_parser.add_argument(
        "--kind",
        choices=("straight", "spin", "return", "restart"),
        required=True,
    )
    record_parser.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS)
    record_parser.set_defaults(func=record)

    straight = commands.add_parser("analyze-straight", help="проанализировать прямой ход")
    straight.add_argument("bag", type=Path)
    straight.add_argument(
        "--distance", type=float, required=True, help="модуль пути по рулетке, м"
    )
    straight.add_argument("--direction", choices=("forward", "reverse"), required=True)
    straight.add_argument("--params", type=Path)
    straight.add_argument("--output", type=Path)
    straight.set_defaults(func=analyze_straight)

    spin = commands.add_parser("analyze-spin", help="проанализировать вращение на месте")
    spin.add_argument("bag", type=Path)
    spin.add_argument("--turns", type=float, required=True, help="число полных оборотов")
    spin.add_argument("--direction", choices=("cw", "ccw"), required=True)
    spin.add_argument("--params", type=Path)
    spin.add_argument("--output", type=Path)
    spin.set_defaults(func=analyze_spin)

    return_test = commands.add_parser(
        "analyze-return", help="проверить возврат вперед-назад к стартовой линии"
    )
    return_test.add_argument("bag", type=Path)
    return_test.add_argument(
        "--physical-offset",
        type=float,
        required=True,
        help="модуль фактического промаха мимо стартовой линии, м",
    )
    return_test.add_argument("--params", type=Path)
    return_test.add_argument("--output", type=Path)
    return_test.set_defaults(func=analyze_return)

    restart = commands.add_parser("analyze-restart", help="проверить odom через перезапуск")
    restart.add_argument("bag", type=Path)
    restart.add_argument("--output", type=Path)
    restart.set_defaults(func=analyze_restart)

    aggregate = commands.add_parser("report", help="собрать итоговый PASS/FAIL")
    aggregate.add_argument("paths", nargs="+", type=Path)
    aggregate.add_argument("--output", type=Path, default=DEFAULT_RUNS / "summary")
    aggregate.set_defaults(func=report)
    return main


def main() -> int:
    """Точка входа."""
    args = parser().parse_args()
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
