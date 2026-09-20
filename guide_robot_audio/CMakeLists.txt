cmake_minimum_required(VERSION 3.8)
project(guide_robot_audio)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(guide_robot_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(rclcpp_lifecycle REQUIRED)
find_package(ALSA REQUIRED)
find_package(Threads REQUIRED)

add_executable(xvf3800_audio_node
  src/xvf3800_audio_node.cpp
)
target_include_directories(xvf3800_audio_node PRIVATE
  ${ALSA_INCLUDE_DIRS}
)
ament_target_dependencies(xvf3800_audio_node
  guide_robot_msgs
  rclcpp
  rclcpp_action
  rclcpp_lifecycle
)
target_link_libraries(xvf3800_audio_node
  ${ALSA_LIBRARIES}
  Threads::Threads
)

install(TARGETS xvf3800_audio_node
  DESTINATION lib/${PROJECT_NAME}
)
install(DIRECTORY config launch
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  set(ament_cmake_uncrustify_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
