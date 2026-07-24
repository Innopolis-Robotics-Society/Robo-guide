cmake_minimum_required(VERSION 3.8)
project(guide_robot_description)

find_package(ament_cmake REQUIRED)

install(
  DIRECTORY urdf params meshes
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
