#pragma once

#include <cstdint>
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
  // -------------------------------------------------------
  // Вспомогательные методы для работы с энкодерами
  // -------------------------------------------------------

  /// Отправить запрос чтения регистра 0xa0 у мотора с заданным ID.
  /// Пакет: ff ff <id> 04 03 a0 05 <chk>
  void sendEncoderRequest(uint8_t motor_id);

  /// Попытаться прочитать один 19-байтный пакет ответа.
  /// Возвращает true если пакет успешно принят и контрольная сумма верна.
  /// enc[0..2] — три int32 из поля DATA (b[6:10], b[10:14], b[14:18]).
  bool parseEncoderResponse(uint8_t motor_id, int32_t enc[3]);

  // -------------------------------------------------------
  // Serial
  // -------------------------------------------------------
  int serial_fd_{-1};
  std::string serial_port_;
  int baud_rate_{115200};

  // Параметры моторов из URDF
  int left_wheel_id_{46};
  int right_wheel_id_{47};
  double left_sign_{1.0};
  double right_sign_{-1.0};
  double speed_coefficient_{0.0008};
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

  double ticks_per_rev_{262144.0};

  // Буфер для неблокирующего чтения ответа энкодеров
  std::vector<uint8_t> rx_buffer_;
  bool initialized_encoders_{false};
  int enc_request_counter_{0};  // счётчик для отправки запросов с пониженной частотой
};

}  // namespace guide_robot_hardware
