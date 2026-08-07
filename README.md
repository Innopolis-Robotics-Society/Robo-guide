# Robo-guide

Autonomous navigation stack for the **Guide-Robot** tour-guide robot (Future Robot Co.), migrating the platform from manual/joystick (pult) control to fully autonomous navigation on **ROS 2 Humble** and LLM models.

Built and maintained by the **Innopolis Robotics Society** team.

## Overview

The original robot ships with `dynrobot` / FUROWEAR (OPRoS middleware, Windows) — not ROS-based. This repo replaces that with a ROS 2 stack targeting **Nav2 + SLAM Toolbox** for mapping and autonomous navigation, plus a voice-guided tour layer (audio I/O, semantic map of exhibits, and a mission FSM that orchestrates `NavigateToPose` + narration + presence-aware pause/resume).

Target stack: ROS 2 Humble · Nav2 · SLAM Toolbox · `ros2_control` + `diff_drive_controller` · `robot_localization` (EKF).

## Packages

| Package | Type | Purpose |
|---------|------|---------|
| [`guide_robot_description`](guide_robot_description/README.md) | CMake (ament) | URDF/Xacro model, `ros2_control` description, robot params, meshes |
| [`guide_robot_hardware`](guide_robot_hardware/README.md) | C++17 (ament_cmake, pluginlib) | `hardware_interface::SystemInterface` plugin bridging the FURO drive base to `ros2_control` |
| [`guide_robot_sonar`](guide_robot_sonar/README.md) | C++/Python (ament) | Low-level serial sonar driver + ROS 2 wrapper nodes publishing `sensor_msgs/Range` per sensor |
| [`guide_robot_msgs`](guide_robot_msgs/README.md) | msg/srv (ament) | Shared interface definitions (currently just `SonarRanges.msg`, largely unused — see package README) |
| [`guide_robot_navigation`](guide_robot_navigation/README.md) | Python (ament) | Nav2 + SLAM Toolbox configuration, maps, launch files, collision monitor |
| [`guide_robot_simulation`](guide_robot_simulation/README.md) | Python (ament) | Gazebo Classic simulation launch, worlds, sonar/sensor plugins |
| [`guide_robot_supervisor`](guide_robot_supervisor/README.md) | Python (ament) | Lifecycle-node supervisor and watchdogs for coordinated bring-up |
| [`guide_robot_voice`](guide_robot_voice/README.md) | Python (ament) | Audio stack: `audio_frontend`, VAD, wakeword, ASR, TTS (`tts_node` exposes the `Say` action) |
| [`guide_robot_semantic_map`](guide_robot_semantic_map/README.md) | Python (ament) | `nav2_route`-backed graph of locations/exhibits/tours, content and routing services |
| [`guide_robot_mission_control`](guide_robot_mission_control/README.md) | Python (ament) | Tour orchestration FSM (`mission_fsm`), narration/barge-in (`narration_server`), presence tracking (`presence_monitor`), `mission_cli` |
| [`guide_robot_llm`](guide_robot_llm/) | Python (ament) | `chat_node` — LLM-backed conversational loop (`/asr/transcript` → LLM → `Say`); not yet wired into the supervisor or the tour FSM |
| [`guide_robot_bringup`](guide_robot_bringup/README.md) | Python (ament) | Top-level launch orchestration (real hardware, simulation, RViz, tour stack) tying all packages together |

Each package now has its own `README.md` with a detailed technical breakdown and a "Известные проблемы" (known issues) section — see the links above for specifics. Note: `.gitignore` excludes `*.md` repo-wide, so these files need `git add -f` to be committed.

## Sensors

- **2× RPLIDAR** — 2D laser scanners for SLAM and obstacle avoidance
- Sonar sensors on the base
- Wheel encoders / IMU — TBD (fallback: `rf2o_laser_odometry` if wheel odometry unavailable)

## Prerequisites

- Ubuntu 22.04
- ROS 2 Humble
- `colcon`, `rosdep`

Or use the containerized environment — see [`.docker/README.md`](.docker/README.md) for the four-image build setup (x86 GPU/CPU, Jetson GPU/CPU).

## Build

```bash
# from the workspace root
rosdep install --from-paths . --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

## Usage

Visualize the robot model in RViz:

```bash
ros2 launch guide_robot_bringup view_robot.launch.py
```

Bring up the hardware interface:

```bash
ros2 launch guide_robot_bringup hardware.launch.py
```

Run the same stack in Gazebo and try a full guided tour — the supervisor
autonomously brings up nav + voice + semantic_map + mission_control, then
`mission_cli` drives a real tour with narration and navigation between
exhibits:

```bash
ros2 launch guide_robot_bringup simulation.launch.py
# wait for `ros2 topic echo /supervisor/state` to report ACTIVE, then:
ros2 run guide_robot_mission_control mission_cli tour --tour lab_demo --no-confirm
```

See [`guide_robot_bringup/README.md`](guide_robot_bringup/README.md#тестовый-сценарий-экскурсии-симуляция)
for the full walkthrough and troubleshooting.

## Docker

```bash
# x86 workstation with CUDA
docker build -f .docker/Dockerfile.x86-gpu -t fabook/iros:x86-gpu .
docker run --gpus all -it fabook/iros:x86-gpu

# lightweight CPU-only (CI / local tests)
docker build -f .docker/Dockerfile.x86-cpu -t fabook/iros:x86-cpu .
```

See [`.docker/README.md`](.docker/README.md) for Jetson builds, bake targets, and per-device build args.

## Roadmap

- [x] Wire up `motor_driver_node` to the real base
- [x] Integrate 2× RPLIDAR + laser scan merging
- [ ] `robot_localization` EKF (encoders/IMU or `rf2o` fallback)
- [x] Nav2 + SLAM Toolbox mapping & navigation
- [x] Tour-guide deployment tuning (glass walls, featureless halls, crowds, docking/charging)
- [x] Voice stack (VAD/wakeword/ASR/TTS) + semantic map of exhibits/tours
- [x] Mission FSM: full tour orchestration (navigate → narrate → confirm), barge-in, pause/resume, safety-hold
- [x] `guide_robot_supervisor` bring-up for the tour layer (voice/semantic_map/mission groups)
- [ ] LLM integration (`guide_robot_llm` exists standalone — not yet wired into the mission FSM or supervisor)

## License

Innopolis Robotics Society
