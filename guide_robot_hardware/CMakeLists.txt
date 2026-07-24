cmake_minimum_required(VERSION 3.8)
project(guide_robot_hardware)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)

find_package(hardware_interface REQUIRED)  # ← добавь
find_package(pluginlib REQUIRED)           # ← добавь
find_package(rclcpp REQUIRED)

add_library(${PROJECT_NAME} SHARED        # ← добавь
  src/guide_robot_system.cpp
)
target_include_directories(${PROJECT_NAME} PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
ament_target_dependencies(${PROJECT_NAME}  # ← добавь
  hardware_interface
  pluginlib
  rclcpp
)
# 3. Зарегистрировать pluginlib плагин
pluginlib_export_plugin_description_file(  # ← добавь
  hardware_interface guide_robot_hardware.xml
)
# 4. Установить
install(TARGETS ${PROJECT_NAME}            # ← добавь
  EXPORT export_${PROJECT_NAME}
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)
install(DIRECTORY include/
  DESTINATION include
)


if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
