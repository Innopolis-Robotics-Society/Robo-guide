# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ROS 2 workspace source tree (`colcon` packages at the repo root) for the **Guide-Robot** tour-guide robot — a ~62 kg differential-drive base that operates around people. Nav2 + SLAM Toolbox + `ros2_control`, plus a voice I/O stack.

**Target runtime is ROS 2 Humble**, running in Docker on a Jetson Orin. The dev host is often a newer distro (e.g. Jazzy) — do **not** verify APIs, service signatures, or parameter names against `/opt/ros/<host-distro>`; several `nav2_msgs` / `controller_manager_msgs` signatures changed between releases. Check Humble docs or the `fabook/iros:*` image.

Comments, docs, and package READMEs are largely in Russian. Match the surrounding language when editing a file.

## Build, test, lint

```bash
# from the workspace root (the repo IS the src tree; build/ install/ log/ are gitignored)
rosdep install --from-paths . --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash

colcon build --packages-select guide_robot_voice   # single package
colcon test  --packages-select guide_robot_voice   # single package tests
colcon test-result --verbose                       # readable failures
```

Pure-Python unit tests (no ROS needed) run directly, but **from inside the package dir** — the package must be importable as `guide_robot_voice.lib.*`:

```bash
cd guide_robot_voice && python3 -m pytest test -q
cd guide_robot_voice && python3 -m pytest test/test_chunker.py::test_name -q   # single test
```

Lint (CI runs `pre-commit` on every PR, plus a full `colcon build`/`colcon test` inside `fabook/iros:x86-cpu`):

```bash
pre-commit install       # hook on every commit
pre-commit run -a        # one-off full pass
```

Python is `ruff` (line-length **99**, pydocstyle pep257, target py310) — configured in the root `pyproject.toml`, with a stricter overlay in `guide_robot_voice/pyproject.toml`. C++ is `clang-format` (`.clang-format`, `.clang-tidy`). `colcon test` also runs `ament_lint_auto` per package.

Container images: `docker buildx bake` (x86 cpu/gpu), `docker buildx bake jetson` for arm64; `compose.yaml` mounts the repo at `~/ros2_ws/src` with `/dev` and PulseAudio passthrough. See `.docker/README.md`.

## Running

```bash
ros2 launch guide_robot_bringup hardware.launch.py     # real robot — top-level entry point
ros2 launch guide_robot_bringup simulation.launch.py   # Gazebo Classic 11
ros2 launch guide_robot_bringup view_robot.launch.py   # URDF/TF only, no ros2_control
ros2 launch guide_robot_voice   tts_only.launch.py     # voice output debug
```

Serial devices are addressed by udev symlink, never by `ttyUSB*`/`ttyACM*`: `/dev/tty_motors`, `/dev/tty_sonar`, `/dev/tty_lidar_left`, `/dev/tty_lidar_right`. Install with `scripts/create_rules.sh` (the sonar/motor rules key on **physical USB port**, since the CH341 adapters share a serial number).

`git-lfs` is required — `.stl`, `.onnx`, `.png`, `.dae`, CAD files are LFS-tracked (`.gitattributes`). A clone without LFS gets pointer files and the voice models / meshes silently break.

## Architecture

### Single source of truth for physical parameters

`guide_robot_description/config/robot_params.yaml` holds every physical constant (`geometry`, `odometry`, `limits`, `drive`, `sensors`, `controller_manager`). It is consumed two ways:

1. **Directly** by `urdf/guide_robot.urdf.xacro` via `xacro.load_yaml()`.
2. **By templating**: `scripts/render_params.py` expands `${section.key}` in `*.yaml.in` → concrete YAML at build time. An unknown key is a build error, not a silent `None`.

Two packages render templates: `guide_robot_description/CMakeLists.txt` (`controllers.yaml.in` → `controllers.yaml`) and `guide_robot_navigation/setup.py`, which `importlib`-loads `render_params.py` **from the sibling source tree** (`../guide_robot_description`) — not from `install/`. Consequence: the packages must stay siblings in one source tree, and `generated_config/` is a derived, gitignored artifact.

If you change a speed limit, wheel radius, or lidar range, change it **here** — never in the generated files or in Nav2 yaml. The trailing `old:` block in `robot_params.yaml` is archival reference from the vendor's `RobotSetting.xml`; nothing reads `old.*`.

### Motion path

```
Nav2 controller_server → /cmd_vel_nav → velocity_smoother → /cmd_vel_smoothed
  → collision_monitor → /diff_drive_controller/cmd_vel_unstamped
  → diff_drive_controller (limiter) → ros2_control → guide_robot_hardware plugin → UART
```

`collision_monitor` is deliberately the **last** stage before the driver and lives under its own `lifecycle_manager_safety`, so the safety layer survives a restart of the main nav stack.

`guide_robot_hardware` is a `pluginlib` `hardware_interface::SystemInterface` (not a node), loaded by `controller_manager`. It is the only place velocity commands become bytes on the bus (FURO protocol — a Dynamixel 1.0 clone with proprietary instruction `0x06`). It carries three independent last-resort guards, all deliberate: a mandatory command watchdog (`cmd_timeout`, refuses to activate without it), a wheel-velocity clamp in `toMotorUnits()` (`command_interface` min/max are metadata only — `hardware_interface` does not enforce them), and an encoder-silence detector that stops the motors and returns `ERROR`. Don't weaken these; the FURO driver holds the last accepted speed forever with no timeout of its own.

`guide_robot.ros2_control.xacro` selects one of three backends by xacro arg: `use_sim` → `gazebo_ros2_control`, `use_mock_hardware` → `mock_components/GenericSystem`, otherwise the real plugin. It is not standalone — it relies on properties defined in `guide_robot.urdf.xacro` before the `xacro:include`.

### Lifecycle orchestration and its known gap

`guide_robot_supervisor` reads a YAML (`config/supervisor.yaml`, `supervisor_slam.yaml`) describing ordered groups (`safety → localization → navigation`) and pluggable watchdogs (dotted-path imported). It gates each group's `STARTUP` on `requires` (other groups ACTIVE) and `preconditions` (named watchdogs at `Level.OK`), then keeps applying policies (`warn`/`pause`/`reset`/`shutdown`/`estop`).

Its whole vocabulary is `nav2_msgs/ManageLifecycleNodes`. **It does not manage `ros2_control`** — `controllers.yaml.in` sets `hardware_components_initial_state: active` as a documented temporary workaround, so the drive comes up with launch, bypassing the supervisor. What actually stops the motors when hardware is absent is `on_configure` failing in the plugin. Any lifecycle manager the supervisor drives must be launched with `autostart: false` (`nav_stack.launch.py` still defaults to `true` — a known contract violation when launched directly rather than through `hardware.launch.py`).

`guide_robot_supervisor/README.md` carries a long, current, numbered list of unfixed defects (restart-loop dribble, `estop` being a no-op with no subscribers, one dead sonar blocking the whole bring-up, etc.). Read it before touching that package — the issues are recorded deliberately, not stale.

### Perception

Two RPLIDAR C1 (right one delayed 5 s to avoid a power sag on simultaneous start) → per-scan `laser_sector_blanker` (blanks hand-calibrated angular sectors where each lidar sees the other's mast; calibrate with `laser_blind_sector_finder`) → `dual_laser_merger` → single `/scan` in `base_footprint`.

**Gotcha:** the merger needs `enable_calibration: True` on real hardware (`lidars.launch.py`) — with `False` both `/scan` and the map come up empty. The `False` in `perception.launch.py` is the *simulation-only* merger instance, where Gazebo already publishes in correct TF frames.

Seven sonars (IDs `1,2,4,5,6,8,9` — vendor wiring numbers, 3 and 7 do not exist) share one UART. `sonar_node_mult.py` is the production node publishing one `sensor_msgs/Range` per sensor on `/sonar/range/<frame_id>` with `SensorDataQoS` (BEST_EFFORT) straight into `collision_monitor`. `sonar_node.py` is the older aggregate-message variant and is launched by nothing.

The pybind11 module `furo_sonars_cpp` is installed into `lib/guide_robot_sonar/`, **not** site-packages, on purpose: `sys.path[0]` is the executed script's dir, so a stale copy left in site-packages by an earlier `PYTHON_INSTALL_DIR` layout cannot shadow the fresh build (colcon never deletes files a build stopped producing). Do not "fix" this by adding `PYTHON_INSTALL_DIR`.

### Voice

`guide_robot_voice` is pure audio I/O — it knows nothing about tours, exhibits, or the LLM. Its only semantics are priority, scope, and **epoch**. `guide_robot_voice/guide_robot_voice_design.md` is the authoritative design document (contract tables, node responsibilities, latency budget, hardware stages, implementation order); consult it before changing topics, QoS, or node boundaries.

Two rules that structure the code:

- **`lib/` never imports `rclpy`** except `qos.py`. That is what makes `chunker`, `scheduler`, `sink`, `resampler`, `ring`, `vad_hysteresis`, `dc_blocker` unit-testable without ROS, and makes the planned Stage-3 C++ rewrite mechanical. Keep new pure logic there.
- **QoS profiles live only in `lib/qos.py`.** A publisher/subscriber QoS mismatch is not an error in ROS 2 — it silently fails to connect.

`epoch` is `clock.now().nanoseconds` from a shared clock, not a per-publisher counter (multiple nodes publish `CancelAll`; per-publisher counters would let a receiver holding `max(epoch)` drop a legitimate cancel). `EpochFencedSink` guarantees at most one already-queued chunk reaches the device after `bump()`; barge-in budget is < 200 ms end to end.

Voice models (`silero_vad.onnx`, `ru_RU-irina-medium.onnx`) live in `models/` under LFS — no separate download step. PulseAudio holds USB audio adapters; suspend the single device (`pactl suspend-source <name> 1`) rather than using `pasuspender`. The production Orin image has no PulseAudio at all, nodes open `hw:` directly.

### Interfaces

`guide_robot_msgs` is the only place `msg`/`srv`/`action` types are defined. The voice/mission contract types (`AudioChunk`, `CancelAll`, `Transcript`, `Wakeword`, `SpeakingStatus`, `Say`, `Narrate`, `RunTour`, `AskUser`, …) live there; adding a type means editing `CMakeLists.txt`'s `rosidl_generate_interfaces` list too.

## Conventions

- Default branch is `dev`; CI runs on push to `dev` and on every PR. Commits are conventional (`feat:`, `fix:`, `refactor:`, `docs:`, sometimes scoped `fix(bringup):`).
- Every package has a detailed `README.md` with a "Известные проблемы" section listing real, unfixed defects with file:line references. These are maintained and worth reading before working in a package — but they can lag the code (e.g. `guide_robot_bringup/README.md` still describes a `sensors.launch.py` that is now split into `perception.launch.py` / `lidars.launch.py` / `nav_stack.launch.py`). Trust the code over the README, and fix the README when you notice drift.
- This robot moves 62 kg among people. Safety-relevant defaults (watchdogs, clamps, collision polygons, AMCL's hardcoded `set_initial_pose`) are commented with the reasoning that produced them — read the comment before changing the value.
