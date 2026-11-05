"""Map pose read/write must use the same navigation-body orientation."""
import math
from types import SimpleNamespace as NS

import pytest
from rclpy.time import Time
from rclpy.clock import ClockType
from guide_robot_api.map_adapter import MapAdapter


@pytest.mark.parametrize("yaw", [0., math.pi / 2, -3.125, -1.366])
def test_observed_pose_matches_submitted_heading(yaw):
    adapter = MapAdapter.__new__(MapAdapter)
    adapter.target = None
    adapter.node = NS(get_clock=lambda: NS(now=lambda: Time(seconds=1, clock_type=ClockType.ROS_TIME)))
    adapter.current = NS(frame="map", metadata=lambda: {}, validate_pose=lambda *a, **kw: None)
    submitted = adapter.pose({"pose": {"x": 2., "y": 3., "yaw": yaw}, "map_version": "v"}, free=False)

    def lookup(frame, body, stamp):
        assert frame == "map"
        assert body == "base_footprint"  # base_link would add pi on this robot
        return NS(header=NS(stamp=Time(seconds=1, clock_type=ClockType.ROS_TIME).to_msg()), transform=NS(
            translation=submitted.pose.position, rotation=submitted.pose.orientation))

    adapter.buffer = NS(lookup_transform=lookup)
    observed = adapter.state()["robot_pose"]
    assert observed["fresh"]
    assert observed["yaw"] == pytest.approx(yaw)
    assert observed["x"] == 2.
