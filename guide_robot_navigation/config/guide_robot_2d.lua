include "map_builder.lua"
include "trajectory_builder.lua"

options = {
  map_builder = MAP_BUILDER,
  trajectory_builder = TRAJECTORY_BUILDER,

  map_frame = "map",
  -- Cartographer rotates linear acceleration into this frame and therefore
  -- requires it to be colocated with the physical IMU.  Robot motion is
  -- still exposed through published_frame="odom" below.
  tracking_frame = "imu_link",

  -- В симуляции diff_drive_controller уже публикует:
  -- odom -> base_footprint
  -- Поэтому Cartographer должен публиковать только map -> odom.
  published_frame = "odom",
  odom_frame = "odom",
  provide_odom_frame = false,

  publish_frame_projected_to_2d = true,
  use_pose_extrapolator = true,

  use_odometry = true,
  use_nav_sat = false,
  use_landmarks = false,

  num_laser_scans = 1,
  num_multi_echo_laser_scans = 0,
  num_subdivisions_per_laser_scan = 1,
  num_point_clouds = 0,

  lookup_transform_timeout_sec = 0.2,
  submap_publish_period_sec = 0.3,
  pose_publish_period_sec = 0.005,
  trajectory_publish_period_sec = 0.03,

  rangefinder_sampling_ratio = 1.0,
  odometry_sampling_ratio = 1.0,
  fixed_frame_pose_sampling_ratio = 1.0,
  imu_sampling_ratio = 1.0,
  landmarks_sampling_ratio = 1.0,
}

MAP_BUILDER.use_trajectory_builder_2d = true

-- Use the 50 Hz IMU from the canonical /imu/data topic.  Gazebo publishes it;
-- hardware must provide the same topic before using this configuration.
TRAJECTORY_BUILDER_2D.use_imu_data = true

-- После dual_laser_merger симуляция выдаёт /scan примерно в этих пределах.
TRAJECTORY_BUILDER_2D.min_range = 0.25
TRAJECTORY_BUILDER_2D.max_range = 12.0
TRAJECTORY_BUILDER_2D.missing_data_ray_length = 12.0

-- Один готовый LaserScan приходит с частотой 10 Гц.
TRAJECTORY_BUILDER_2D.num_accumulated_range_data = 1
TRAJECTORY_BUILDER_2D.submaps.num_range_data = 90

POSE_GRAPH.optimize_every_n_nodes = 40

return options
