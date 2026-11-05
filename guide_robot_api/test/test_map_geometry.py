"""Map geometry and image contracts without a running ROS graph."""

import math
from types import SimpleNamespace as NS

import pytest
from guide_robot_api.lib.map_geometry import OccupancyMap


def grid():
    return NS(
        header=NS(frame_id="map"),
        info=NS(
            width=2,
            height=2,
            resolution=0.5,
            origin=NS(
                position=NS(x=2.0, y=3.0),
                orientation=NS(x=0, y=0, z=math.sin(math.pi / 4), w=math.cos(math.pi / 4)),
            ),
        ),
        data=[0, 100, -1, 0],
    )


def test_rotated_origin_and_target_validation():
    m = OccupancyMap(grid())
    pose = {"x": 1.75, "y": 3.25, "yaw": 0.0}
    assert m.cell(pose["x"], pose["y"]) == (0, 0)
    m.validate_pose(pose, m.version, free=True)
    with pytest.raises(ValueError, match="map_changed"):
        m.validate_pose(pose, "old", free=True)
    with pytest.raises(ValueError, match="target_not_free"):
        m.validate_pose({**pose, "y": 3.75}, m.version, free=True)
    with pytest.raises(ValueError, match="invalid_pose"):
        m.validate_pose({**pose, "yaw": float("nan")}, m.version)


def test_version_and_png():
    m = OccupancyMap(grid())
    assert m.version == OccupancyMap(grid()).version
    changed = grid()
    changed.data[0] = 100
    assert m.version != OccupancyMap(changed).version
    assert m.png().startswith(b"\x89PNG\r\n\x1a\n")
    assert m.metadata()["origin"]["yaw"] == pytest.approx(math.pi / 2)
