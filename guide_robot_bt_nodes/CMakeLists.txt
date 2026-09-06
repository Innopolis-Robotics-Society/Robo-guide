cmake_minimum_required(VERSION 3.8)
project(guide_robot_bt_nodes)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# ── Зависимости ───────────────────────────────────────────────────────────────
find_package(ament_cmake REQUIRED)
find_package(behaviortree_cpp_v3 REQUIRED)
find_package(guide_robot_msgs REQUIRED)
find_package(nav2_behavior_tree REQUIRED)
find_package(pluginlib REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)

# ── Плагин SayAction ──────────────────────────────────────────────────────────
# Собирается как shared library: bt_navigator загружает его через pluginlib
# по записи в plugin_lib_names (first_iter_nav2.yaml → say_action_bt_node).
add_library(say_action_bt_node SHARED
  src/say_action.cpp
)

target_include_directories(say_action_bt_node PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

ament_target_dependencies(say_action_bt_node
  behaviortree_cpp_v3
  guide_robot_msgs
  nav2_behavior_tree
  rclcpp
  rclcpp_action
)

# Экспорт плагина для pluginlib / bt_navigator
pluginlib_export_plugin_description_file(nav2_behavior_tree
  nav2_tree_nodes.xml
)

# ── Установка ─────────────────────────────────────────────────────────────────
install(TARGETS say_action_bt_node
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

install(FILES nav2_tree_nodes.xml
  DESTINATION share/${PROJECT_NAME}
)

# ── Lint (CI) ─────────────────────────────────────────────────────────────────
if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
