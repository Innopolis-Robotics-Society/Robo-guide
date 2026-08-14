"""Pure geometry for scan_merger: homogeneous transforms and LaserScan binning.

Kept ROS-free so the math is unit-testable without rclpy (same pattern as
guide_robot_voice/lib): the node feeds these helpers numpy arrays and plain
floats, never ROS messages.
"""

import math

import numpy as np


def polar_to_points(ranges, angle_min, angle_increment, range_min, range_max):
    """Convert LaserScan polar ranges to (N, 3) xyz points (z=0) in the scan frame.

    Rays that are non-finite or outside [range_min, range_max] are dropped.
    """
    r = np.asarray(ranges, dtype=np.float64)
    angles = angle_min + np.arange(r.shape[0], dtype=np.float64) * angle_increment
    valid = np.isfinite(r) & (r >= range_min) & (r <= range_max)
    r = r[valid]
    angles = angles[valid]
    return np.column_stack((r * np.cos(angles), r * np.sin(angles), np.zeros(r.shape[0])))


def quat_to_matrix(qx, qy, qz, qw):
    """Quaternion (x, y, z, w) -> 3x3 rotation matrix."""
    n = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if n < 1e-12:
        return np.eye(3)
    qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
    return np.array(
        [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
        ]
    )


def make_transform(translation_xyz, quaternion_xyzw):
    """Build a 4x4 homogeneous transform from translation (x, y, z) and quaternion (xyzw)."""
    t = np.eye(4)
    t[:3, :3] = quat_to_matrix(*quaternion_xyzw)
    t[:3, 3] = np.asarray(translation_xyz, dtype=np.float64)
    return t


def make_planar_transform(x, y, yaw):
    """Build a 4x4 transform for a planar (x, y, yaw) offset (per-lidar calibration)."""
    c, s = math.cos(yaw), math.sin(yaw)
    t = np.eye(4)
    t[0, 0], t[0, 1], t[1, 0], t[1, 1] = c, -s, s, c
    t[0, 3], t[1, 3] = x, y
    return t


def apply_transform(points, matrix):
    """Apply a 4x4 homogeneous transform to (N, 3) points."""
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def deskew_delta(t_odom_base_src, t_odom_base_dst):
    """Return the 4x4 transform mapping base-frame points from t_src to t_dst.

    t_odom_base_* are odom<-base_footprint transforms at the two instants.
    p_base(dst) = inv(T_odom_base(dst)) @ T_odom_base(src) @ p_base(src) for a
    world-fixed point, so the return value relocates a whole cloud measured at
    src into the base pose of dst.
    """
    return np.linalg.inv(t_odom_base_dst) @ t_odom_base_src


def bin_points(points, angle_min, angle_max, angle_increment, range_min, range_max, fill_value):
    """Bin (N, 3) points into a LaserScan ranges row; the nearest point wins each bin.

    Bins with no point get fill_value (inf for use_inf=True, range_max+epsilon
    otherwise). Angular limits follow LaserScan convention: both ends inclusive.
    """
    count = int(math.ceil((angle_max - angle_min) / angle_increment))
    ranges = np.full(count, fill_value, dtype=np.float64)
    if points.shape[0] == 0:
        return ranges
    planar = np.hypot(points[:, 0], points[:, 1])
    angles = np.arctan2(points[:, 1], points[:, 0])
    ok = (
        (planar >= range_min)
        & (planar <= range_max)
        & (angles >= angle_min)
        & (angles <= angle_max)
    )
    if not np.any(ok):
        return ranges
    planar = planar[ok]
    idx = ((angles[ok] - angle_min) / angle_increment).astype(np.int64)
    idx = np.clip(idx, 0, count - 1)
    # Assign in descending range order so the smallest range lands last.
    order = np.argsort(planar, kind="stable")[::-1]
    ranges[idx[order]] = planar[order]
    return ranges
