#include "guide_robot_hardware/guide_robot_system.hpp"

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

#include <cstring>

#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "rclcpp/rclcpp.hpp"

namespace guide_robot_hardware {
// read
hardware_interface::CallbackReturn GuideRobotSystem::on_init(
  const hardware_interface::HardwareInfo & info)
{
  if (
    hardware_interface::SystemInterface::on_init(info) !=
    hardware_interface::CallbackReturn::SUCCESS) {
    return hardware_interface::CallbackReturn::ERROR;
  }
  serial_port_ = info_.hardware_parameters.at("serial_port");
  baud_rate_ = std::stoi(info_.hardware_parameters.at("baud_rate"));
  left_wheel_id_ = std::stoi(info_.hardware_parameters.at("left_wheel_id"));
  right_wheel_id_ = std::stoi(info_.hardware_parameters.at("right_wheel_id"));
  left_sign_ = std::stod(info_.hardware_parameters.at("left_sign"));
  right_sign_ = std::stod(info_.hardware_parameters.at("right_sign"));
  speed_coefficient_ = std::stod(info_.hardware_parameters.at("speed_coefficient"));
  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Параметры: port=%s baud=%d L_id=%d R_id=%d coeff=%.4f",
    serial_port_.c_str(), baud_rate_, left_wheel_id_, right_wheel_id_, speed_coefficient_);

  clock_ = std::make_shared<rclcpp::Clock>(RCL_STEADY_TIME);

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_configure(const rclcpp_lifecycle::State &)
{
  // Открываем порт: O_RDWR=чтение+запись, O_NOCTTY=не делать терминалом, O_SYNC=синхронная запись
  serial_fd_ = open(serial_port_.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
  if (serial_fd_ < 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "Не удалось открыть порт: %s", serial_port_.c_str());
    return hardware_interface::CallbackReturn::ERROR;
  }

  // Настраиваем параметры порта (скорость, биты данных и т.д.)
  struct termios tty;
  memset(&tty, 0, sizeof(tty));
  tcgetattr(serial_fd_, &tty);  // читаем текущие настройки

  cfsetospeed(&tty, B115200);  // скорость передачи
  cfsetispeed(&tty, B115200);

  tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;  // 8 бит данных
  tty.c_cflag |= (CLOCAL | CREAD);             // включить приём

  tcsetattr(serial_fd_, TCSANOW, &tty);  // применить настройки

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Serial порт открыт: %s", serial_port_.c_str());
  return hardware_interface::CallbackReturn::SUCCESS;
}
hardware_interface::CallbackReturn GuideRobotSystem::on_activate(const rclcpp_lifecycle::State &)
{
  // Отправляем Torque Enable (регистр 0x18 = 1) каждому мотору.
  // Без этого контроллер игнорирует команды скорости.
  auto send_torque_enable = [&](int motor_id) {
    uint8_t id = static_cast<uint8_t>(motor_id);
    // Пакет: FF FF <ID> 04 03 18 01 <CHK>
    // 0x03 = WRITE DATA, 0x18 = Torque Enable register, 0x01 = включить
    uint8_t body[5] = {id, 0x04, 0x03, 0x18, 0x01};
    uint8_t checksum = 0;
    for (auto b : body) checksum += b;
    checksum = ~checksum;

    uint8_t packet[8];
    packet[0] = 0xFF;
    packet[1] = 0xFF;
    std::memcpy(&packet[2], body, 5);
    packet[7] = checksum;

    ::write(serial_fd_, packet, sizeof(packet));
    // Небольшая пауза чтобы контроллер обработал команду
    usleep(10000);  // 10 мс
  };

  send_torque_enable(left_wheel_id_);
  send_torque_enable(right_wheel_id_);

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Torque Enable отправлен моторам L=%d R=%d",
    left_wheel_id_, right_wheel_id_);

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_deactivate(const rclcpp_lifecycle::State &)
{
  left_vel_cmd_ = 0.0;
  right_vel_cmd_ = 0.0;
  // write() вызовется ещё раз с нулями — отправит стоп на моторы
  if (serial_fd_ >= 0) {
    close(serial_fd_);
    serial_fd_ = -1;
  }
  return hardware_interface::CallbackReturn::SUCCESS;
}
std::vector<hardware_interface::StateInterface> GuideRobotSystem::export_state_interfaces()
{
  std::vector<hardware_interface::StateInterface> state_interfaces;
  // Левое колесо: даём доступ к position и velocity
  state_interfaces.emplace_back(
    "left_wheel_joint",                  // имя joint (как в URDF)
    hardware_interface::HW_IF_POSITION,  // тип = "position"
    &left_position_                      // указатель на нашу переменную
  );
  state_interfaces.emplace_back(
    "left_wheel_joint",
    hardware_interface::HW_IF_VELOCITY,  // тип = "velocity"
    &left_velocity_);
  // Правое колесо аналогично
  state_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_POSITION, &right_position_);
  state_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_VELOCITY, &right_velocity_);
  return state_interfaces;
}

std::vector<hardware_interface::CommandInterface> GuideRobotSystem::export_command_interfaces()
{
  std::vector<hardware_interface::CommandInterface> command_interfaces;
  // Принимаем velocity команды для двух колёс
  command_interfaces.emplace_back(
    "left_wheel_joint",
    hardware_interface::HW_IF_VELOCITY,  // тип = "velocity"
    &left_vel_cmd_                       // сюда diff_drive_controller пишет рад/с
  );
  command_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_VELOCITY, &right_vel_cmd_);
  return command_interfaces;
}

hardware_interface::return_type GuideRobotSystem::read(
  const rclcpp::Time &, const rclcpp::Duration &)
{
  return hardware_interface::return_type::OK;
}

hardware_interface::return_type GuideRobotSystem::write(
  const rclcpp::Time &, const rclcpp::Duration &)
{
  if (serial_fd_ < 0) {
    return hardware_interface::return_type::OK;  // порт не открыт — молчим
  }

  // 1. Конвертация рад/с → motor units
  //    v (м/с) = omega (рад/с) * wheel_radius
  //    units   = v / speed_coefficient
  auto to_motor_units = [&](double omega, double sign) -> int16_t {
    double v = omega * wheel_radius_;
    double units = sign * v / speed_coefficient_;
    // Ограничиваем диапазон int16
    if (units > 32767) units = 32767;
    if (units < -32768) units = -32768;
    return static_cast<int16_t>(units);
  };

  int16_t l_spd = to_motor_units(left_vel_cmd_, left_sign_);
  int16_t r_spd = to_motor_units(right_vel_cmd_, right_sign_);

  constexpr uint16_t ACCEL = 1000;

  // 2. Собираем пакет (body — от 0xFE до последнего байта данных)
  uint8_t body[15] = {
    0xFE,  // Broadcast ID (команду слышат все драйверы на шине)
    0x0E,  // Длина пакета
    0x06,  // Проприетарная инструкция Future Robot (аналог Sync Write)
    0x20,  // Адрес стартового регистра
    0x04,  // Кол-во байт данных на один мотор
    static_cast<uint8_t>(left_wheel_id_),
    static_cast<uint8_t>(l_spd & 0xFF),         // speed low byte
    static_cast<uint8_t>((l_spd >> 8) & 0xFF),  // speed high byte
    static_cast<uint8_t>(ACCEL & 0xFF),
    static_cast<uint8_t>((ACCEL >> 8) & 0xFF),
    static_cast<uint8_t>(right_wheel_id_),
    static_cast<uint8_t>(r_spd & 0xFF),
    static_cast<uint8_t>((r_spd >> 8) & 0xFF),
    static_cast<uint8_t>(ACCEL & 0xFF),
    static_cast<uint8_t>((ACCEL >> 8) & 0xFF),
  };

  // 3. Checksum: (~sum(body)) & 0xFF
  uint8_t checksum = 0;
  for (auto b : body) checksum += b;
  checksum = ~checksum;  // uint8_t — автоматически & 0xFF

  // 4. Итоговый пакет: [0xFF, 0xFF] + body + [checksum]
  uint8_t packet[18];
  packet[0] = 0xFF;
  packet[1] = 0xFF;
  std::memcpy(&packet[2], body, 15);
  packet[17] = checksum;

  // 5. Отправка
  ::write(serial_fd_, packet, sizeof(packet));

  // Всегда логируем (раз в 2с) — для диагностики
  RCLCPP_INFO_THROTTLE(
    rclcpp::get_logger("GuideRobotSystem"), *clock_, 2000,
    "[write] L_cmd=%.3f rad/s -> units=%d | R_cmd=%.3f rad/s -> units=%d | fd=%d", left_vel_cmd_,
    l_spd, right_vel_cmd_, r_spd, serial_fd_);

  return hardware_interface::return_type::OK;
}

}  // namespace guide_robot_hardware

#include "pluginlib/class_list_macros.hpp"
PLUGINLIB_EXPORT_CLASS(guide_robot_hardware::GuideRobotSystem, hardware_interface::SystemInterface)
