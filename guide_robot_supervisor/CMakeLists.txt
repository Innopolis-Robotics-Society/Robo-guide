cmake_minimum_required(VERSION 3.8)
project(guide_robot_supervisor)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
  set(CMAKE_CXX_STANDARD_REQUIRED ON)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# ── Зависимости ───────────────────────────────────────────────────────────────
find_package(ament_cmake REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(nav2_msgs REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(tf2_msgs REQUIRED)
find_package(yaml_cpp_vendor REQUIRED)

# ── Конфиг: без ROS, только yaml-cpp ──────────────────────────────────────────
add_library(supervisor_config STATIC
  src/config.cpp
  src/rate_estimator.cpp
  src/status.cpp
)
target_include_directories(supervisor_config PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(supervisor_config PUBLIC yaml-cpp)
set_target_properties(supervisor_config PROPERTIES POSITION_INDEPENDENT_CODE ON)

# ── Watchdog'и ────────────────────────────────────────────────────────────────
add_library(supervisor_watchdogs STATIC
  src/watchdog.cpp
  src/watchdogs/lifecycle_active.cpp
  src/watchdogs/node_alive.cpp
  src/watchdogs/tf_edge.cpp
  src/watchdogs/topic_rate.cpp
)
target_link_libraries(supervisor_watchdogs PUBLIC supervisor_config)
ament_target_dependencies(supervisor_watchdogs PUBLIC rclcpp std_srvs tf2_msgs)
set_target_properties(supervisor_watchdogs PROPERTIES POSITION_INDEPENDENT_CODE ON)

# ── Узел ──────────────────────────────────────────────────────────────────────
add_library(supervisor_core STATIC src/supervisor.cpp)
target_link_libraries(supervisor_core PUBLIC supervisor_watchdogs)
ament_target_dependencies(supervisor_core PUBLIC diagnostic_msgs nav2_msgs rclcpp std_msgs std_srvs)

add_executable(supervisor src/main.cpp)
target_link_libraries(supervisor supervisor_core)
install(TARGETS supervisor DESTINATION lib/${PROJECT_NAME})

install(DIRECTORY config launch DESTINATION share/${PROJECT_NAME})

# ── Тесты ─────────────────────────────────────────────────────────────────────
if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(sensor_msgs REQUIRED)

  ament_add_gtest(test_config test/test_config.cpp
    ENV CONFIG_DIR=${CMAKE_CURRENT_SOURCE_DIR}/config)
  target_link_libraries(test_config supervisor_config)

  ament_add_gtest(test_rate_estimator test/test_rate_estimator.cpp)
  target_link_libraries(test_rate_estimator supervisor_config)

  ament_add_gtest(test_watchdogs test/test_watchdogs.cpp TIMEOUT 120 SKIP_LINKING_MAIN_LIBRARIES)
  target_link_libraries(test_watchdogs supervisor_watchdogs)
  ament_target_dependencies(test_watchdogs sensor_msgs)

  ament_add_gtest(test_supervisor test/test_supervisor.cpp TIMEOUT 300 SKIP_LINKING_MAIN_LIBRARIES)
  target_link_libraries(test_supervisor supervisor_core)
  ament_target_dependencies(test_supervisor sensor_msgs)
endif()

ament_package()
