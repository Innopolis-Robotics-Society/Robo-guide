cmake_minimum_required(VERSION 3.8)
project(guide_robot_sonar)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
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

# Install the bindings next to the scripts that import them, not into
# site-packages. Two reasons:
#   * No hardcoded python3.10 path and no dependency on where
#     PYTHON_INSTALL_DIR happens to resolve on this distro.
#   * sys.path[0] is the executed script's own directory, so it wins over
#     every PYTHONPATH entry. A stale furo_sonars_cpp left in site-packages
#     by an earlier install (colcon never deletes files a build stopped
#     producing) therefore cannot shadow the freshly built one — which it
#     did, silently, when this target moved between install paths.
# Nothing outside this package imports furo_sonars_cpp, so narrowing its
# visibility costs nothing.
install(TARGETS furo_sonars_cpp
  DESTINATION lib/${PROJECT_NAME}
)

# Python scripts. sonar_mapping.py is not an entry point — it's imported by
# the two scripts above and resolves through the same sys.path[0].
install(PROGRAMS
  scripts/sonar_node.py
  scripts/sonar_node_mult.py
  DESTINATION lib/${PROJECT_NAME}
)
install(FILES
  scripts/sonar_mapping.py
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
