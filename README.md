# Robo-guide

Autonomous navigation stack for the **Guide-Robot** tour-guide robot (Future Robot Co.), migrating the platform from manual/joystick (pult) control to fully autonomous navigation on **ROS 2 Humble** and LLM models.

Built and maintained by the **Innopolis Robotics Society** team.

## Overview

The original robot ships with `dynrobot` / FUROWEAR (OPRoS middleware, Windows) — not ROS-based. This repo replaces that with a ROS 2 stack targeting **Nav2 + SLAM Toolbox** for mapping and autonomous navigation.

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
| [`guide_robot_bringup`](guide_robot_bringup/README.md) | Python (ament) | Top-level launch orchestration (real hardware, simulation, RViz) tying all packages together |

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
- [ ] LLM integration

## License

Innopolis Robotics Society
