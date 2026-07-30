"""Sonar ID -> URDF frame mapping, shared by sonar_node.py and sonar_node_mult.py.

Single source of truth: driver sonar IDs follow the original Guide-Robot
harness labels, not poll order. These must match the sonar link names in
guide_robot_description/urdf/guide_robot.urdf.xacro.
"""

SONAR_MAPPING = {
    0: "sonar_sensor_1",  # Front Left
    1: "sonar_sensor_2",  # Left Front
    2: "sonar_sensor_4",  # Left Rear
    3: "sonar_sensor_5",  # Rear Center
    4: "sonar_sensor_6",  # Right Rear
    5: "sonar_sensor_8",  # Right Front
    6: "sonar_sensor_9",  # Front Right
}
