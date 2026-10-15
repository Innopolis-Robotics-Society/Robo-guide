#include <exception>
#include <memory>

#include "guide_robot_supervisor/supervisor.hpp"
#include "rclcpp/rclcpp.hpp"

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);

  std::shared_ptr<guide_robot_supervisor::Supervisor> node;
  try {
    node = std::make_shared<guide_robot_supervisor::Supervisor>();
  } catch (const std::exception & exc) {
    // ConfigError уже содержит файл, секцию и строку
    RCLCPP_FATAL(rclcpp::get_logger("supervisor"), "cannot start: %s", exc.what());
    rclcpp::shutdown();
    return 1;
  }

  // Все колбэки короткие (команды уходят в очередь рабочего потока), одного
  // потока executor'а хватает.
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node);
  node->start();
  executor.spin();

  executor.remove_node(node);
  node->stop();
  node.reset();
  rclcpp::shutdown();
  return 0;
}
