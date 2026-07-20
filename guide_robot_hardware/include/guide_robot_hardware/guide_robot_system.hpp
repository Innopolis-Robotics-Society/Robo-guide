#pragma once

#include <string>
#include <vector>

#include "hardware_interface/handle.hpp"
#include "hardware_interface/hardware_info.hpp"
#include "hardware_interface/system_interface.hpp"
#include "hardware_interface/types/hardware_interface_return_values.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_lifecycle/state.hpp"

namespace guide_robot_hardware {

class GuideRobotSystem : public hardware_interface::SystemInterface
{
public:
  RCLCPP_SHARED_PTR_DEFINITIONS(GuideRobotSystem)

  // Жизненный цикл
  hardware_interface::CallbackReturn on_init(
    const hardware_interface::HardwareInfo & info) override;

  hardware_interface::CallbackReturn on_configure(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_activate(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_deactivate(
    const rclcpp_lifecycle::State & previous_state) override;

  // Интерфейсы состояния и команд
  std::vector<hardware_interface::StateInterface> export_state_interfaces() override;
  std::vector<hardware_interface::CommandInterface> export_command_interfaces() override;

  // Основной цикл
  hardware_interface::return_type read(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

  hardware_interface::return_type write(
    const rclcpp::Time & time, const rclcpp::Duration & period) override;

private:
  // Serial
  int serial_fd_{-1};
  std::string serial_port_;
  int baud_rate_{115200};

  // Параметры моторов из URDF
  int left_wheel_id_{46};
  int right_wheel_id_{47};
  double left_sign_{-1.0};
  double right_sign_{1.0};
  double speed_coefficient_{0.014};
  double wheel_radius_{0.075};

  // Clock для RCLCPP_INFO_THROTTLE (должен жить дольше вызова макроса)
  rclcpp::Clock::SharedPtr clock_;

  // Команды (пишет controller_manager)
  double left_vel_cmd_{0.0};   // рад/с
  double right_vel_cmd_{0.0};  // рад/с

  // Состояния (читает controller_manager)
  double left_position_{0.0};
  double right_position_{0.0};
  double left_velocity_{0.0};
  double right_velocity_{0.0};
};

}  // namespace guide_robot_hardware
