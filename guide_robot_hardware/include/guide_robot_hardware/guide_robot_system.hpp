#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <string>
#include <thread>
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

  ~GuideRobotSystem() override;

  // Жизненный цикл
  hardware_interface::CallbackReturn on_init(
    const hardware_interface::HardwareInfo & info) override;

  hardware_interface::CallbackReturn on_configure(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_activate(
    const rclcpp_lifecycle::State & previous_state) override;

  hardware_interface::CallbackReturn on_deactivate(
    const rclcpp_lifecycle::State & previous_state) override;

  // Порт открывается в on_configure, поэтому закрывается СИММЕТРИЧНО здесь,
  // а не в on_deactivate: deactivate→activate — штатная операция
  // controller_manager, после неё драйвер обязан продолжать работать.
  hardware_interface::CallbackReturn on_cleanup(
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
  // Протокол
  // -------------------------------------------------------

  /// Отправить запрос чтения регистра 0xa0 у мотора left_wheel_id_.
  /// Пакет: ff ff <id> 04 03 a0 05 <chk>, контрольная сумма считается по факту.
  void sendEncoderRequest();

  /// Собрать и отправить 18-байтный пакет скоростей (слоты адресуются по позиции).
  /// Возвращает false, если пакет ушёл не полностью.
  bool writeSpeedPacket(double slot1_cmd, double slot2_cmd);

  /// рад/с → motor units с учётом знака конкретного слота.
  int16_t toMotorUnits(double omega, double sign) const;

  /// Сторожевой таймер: шлёт нулевые скорости, если write() не вызывался
  /// дольше cmd_timeout_ (зависший или убитый управляющий цикл).
  void watchdogLoop();
  void startWatchdog();
  void stopWatchdog();

  /// Остановить моторы и закрыть порт. Идемпотентно.
  void closePort();

  // -------------------------------------------------------
  // Serial
  // -------------------------------------------------------
  int serial_fd_{-1};
  std::string serial_port_;
  int baud_rate_{115200};

  // Единственный мьютекс на все ::write в порт: watchdogLoop() пишет из своего
  // потока параллельно с read()/write() управляющего цикла.
  std::mutex serial_mutex_;

  // Параметры моторов из URDF
  int left_wheel_id_{46};
  int right_wheel_id_{47};
  bool swap_drives_{false};
  double left_sign_{1.0};
  double right_sign_{-1.0};
  double speed_coefficient_{0.0001706};
  double wheel_radius_{0.1026};
  double cmd_timeout_{0.0};  // с; 0 — сторож выключен

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

  double ticks_per_rev_{131072.0};

  // Буфер для неблокирующего чтения ответа энкодеров
  std::vector<uint8_t> rx_buffer_;
  bool initialized_encoders_{false};
  int enc_request_counter_{0};  // счётчик для отправки запросов с пониженной частотой

  // Время, накопленное с прошлого УСПЕШНО разобранного пакета энкодеров.
  // Кадр приходит раз в 5 циклов, поэтому делить дельту позиции на период
  // одного цикла нельзя — скорость завышалась впятеро.
  double enc_elapsed_{0.0};

  // Сторожевой таймер
  std::thread watchdog_thread_;
  std::atomic<bool> watchdog_running_{false};
  std::atomic<int64_t> last_write_ns_{0};  // steady_clock, момент последнего write()
};

}  // namespace guide_robot_hardware
