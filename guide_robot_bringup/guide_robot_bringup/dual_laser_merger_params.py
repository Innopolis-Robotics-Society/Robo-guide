"""Shared dual_laser_merger parameter dict for hardware and sim launches."""

import math
import os

import yaml
from ament_index_python.packages import get_package_share_directory


def lidar_range_max():
    """Return RPLIDAR C1 range from robot_params.yaml (same key AMCL templates use)."""
    path = os.path.join(
        get_package_share_directory("guide_robot_description"),
        "config",
        "robot_params.yaml",
    )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["sensors"]["lidar_range_max"]


def merger_params(
    *,
    laser_1_topic,
    laser_2_topic,
    target_frame,
    enable_calibration,
    **offsets,
):
    """Return base merger params; overlay topics, calibration flag, and ICP offsets."""
    params = {
        "laser_1_topic": laser_1_topic,
        "laser_2_topic": laser_2_topic,
        "target_frame": target_frame,
        "tolerance": 0.05,
        "queue_size": 10,
        "angle_increment": 0.005,
        "scan_time": 0.1,
        "range_min": 0.1,
        "range_max": lidar_range_max(),
        "min_height": -0.5,
        "max_height": 1.5,
        "angle_min": -math.pi,
        "angle_max": math.pi,
        "use_inf": True,
        "inf_epsilon": 1.0,
        "enable_calibration": enable_calibration,
        "enable_average_filter": False,
        "enable_shadow_filter": False,
    }
    params.update(offsets)
    return params
