cmake_minimum_required(VERSION 3.8)
project(guide_robot_sonar)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(guide_robot_msgs REQUIRED)
find_package(Python3 COMPONENTS Development REQUIRED)
find_package(pybind11 REQUIRED)

# include directories
include_directories(include)

# create python binding library
pybind11_add_module(furo_sonars_cpp SHARED
  src/sonar_driver.cpp
  src/sonar_driver_bindings.cpp
)
target_link_libraries(furo_sonars_cpp PRIVATE
  rclcpp::rclcpp
)

# Install shared library python bindings to python package directory
install(TARGETS furo_sonars_cpp
  DESTINATION lib/python3.10/site-packages
)

# Install Python scripts
install(PROGRAMS
  scripts/sonar_node.py
  scripts/sonar_node_mult.py
  DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips uncrustify since we use clang-format in pre-commit
  set(ament_cmake_uncrustify_FOUND TRUE)
  # the following line skips cpplint
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
