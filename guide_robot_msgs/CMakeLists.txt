cmake_minimum_required(VERSION 3.8)
project(guide_robot_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(action_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/NarrationToken.msg"
  "msg/MissionState.msg"
  "msg/SystemEvent.msg"
  "msg/Transcript.msg"
  "msg/Location.msg"
  "srv/ListLocations.srv"
  "srv/EstimateRoute.srv"
  "srv/GetExhibitContent.srv"
  "action/Narrate.action"
  "action/RunTour.action"
  "action/AskUser.action"
  "action/Say.action"
  "msg/SonarRanges.msg"
  DEPENDENCIES builtin_interfaces std_msgs geometry_msgs sensor_msgs action_msgs
)

ament_export_dependencies(rosidl_default_runtime)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()