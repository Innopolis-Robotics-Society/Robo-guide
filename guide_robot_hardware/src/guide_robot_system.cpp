#include "guide_robot_hardware/guide_robot_system.hpp"

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

#include <cstdint>
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
  if (info_.hardware_parameters.count("wheel_radius") > 0) {
    wheel_radius_ = std::stod(info_.hardware_parameters.at("wheel_radius"));
  }
  if (info_.hardware_parameters.count("ticks_per_rev") > 0) {
    ticks_per_rev_ = std::stod(info_.hardware_parameters.at("ticks_per_rev"));
  }
  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"),
    "Параметры: port=%s baud=%d L_id=%d R_id=%d coeff=%.4f r=%.3fm ticks_per_rev=%.1f",
    serial_port_.c_str(), baud_rate_, left_wheel_id_, right_wheel_id_, speed_coefficient_,
    wheel_radius_, ticks_per_rev_);

  clock_ = std::make_shared<rclcpp::Clock>(RCL_STEADY_TIME);

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_configure(const rclcpp_lifecycle::State &)
{
  // O_SYNC: write() блокирует до полной передачи — надёжно для RS-485 half-duplex.
  // Без O_NONBLOCK на весь fd: read() управляем через VMIN/VTIME в termios.
  serial_fd_ = open(serial_port_.c_str(), O_RDWR | O_NOCTTY | O_SYNC);
  if (serial_fd_ < 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "Не удалось открыть порт: %s", serial_port_.c_str());
    return hardware_interface::CallbackReturn::ERROR;
  }

  // Настраиваем 115200 8N1, raw mode
  struct termios tty;
  memset(&tty, 0, sizeof(tty));
  tcgetattr(serial_fd_, &tty);

  cfsetospeed(&tty, B115200);
  cfsetispeed(&tty, B115200);

  tty.c_cflag = (tty.c_cflag & ~CSIZE) | CS8;  // 8 бит данных
  tty.c_cflag |= (CLOCAL | CREAD);             // включить приём
  tty.c_cflag &= ~(PARENB | CSTOPB);           // без чётности, 1 стоп-бит

  tty.c_iflag = 0;  // raw input: без software flow control, без специальных символов
  tty.c_lflag = 0;  // raw mode: без эха и канонического режима
  tty.c_oflag = 0;  // raw output

  // VMIN=0, VTIME=0: read() возвращается немедленно если данных нет (0 байт).
  // Это эквивалент неблокирующего read() без O_NONBLOCK на весь fd.
  tty.c_cc[VMIN] = 0;
  tty.c_cc[VTIME] = 0;

  tcsetattr(serial_fd_, TCSANOW, &tty);

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Serial порт открыт (O_SYNC, VMIN=0): %s",
    serial_port_.c_str());
  return hardware_interface::CallbackReturn::SUCCESS;
}
hardware_interface::CallbackReturn GuideRobotSystem::on_activate(const rclcpp_lifecycle::State &)
{
  rx_buffer_.clear();
  initialized_encoders_ = false;

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "GuideRobotSystem активирован (моторы L=%d R=%d)",
    left_wheel_id_, right_wheel_id_);

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_deactivate(const rclcpp_lifecycle::State &)
{
  left_vel_cmd_ = 0.0;
  right_vel_cmd_ = 0.0;
  if (serial_fd_ >= 0) {
    close(serial_fd_);
    serial_fd_ = -1;
  }
  return hardware_interface::CallbackReturn::SUCCESS;
}
std::vector<hardware_interface::StateInterface> GuideRobotSystem::export_state_interfaces()
{
  std::vector<hardware_interface::StateInterface> state_interfaces;
  state_interfaces.emplace_back(
    "left_wheel_joint", hardware_interface::HW_IF_POSITION, &left_position_);
  state_interfaces.emplace_back(
    "left_wheel_joint", hardware_interface::HW_IF_VELOCITY, &left_velocity_);
  state_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_POSITION, &right_position_);
  state_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_VELOCITY, &right_velocity_);
  return state_interfaces;
}

std::vector<hardware_interface::CommandInterface> GuideRobotSystem::export_command_interfaces()
{
  std::vector<hardware_interface::CommandInterface> command_interfaces;
  command_interfaces.emplace_back(
    "left_wheel_joint", hardware_interface::HW_IF_VELOCITY, &left_vel_cmd_);
  command_interfaces.emplace_back(
    "right_wheel_joint", hardware_interface::HW_IF_VELOCITY, &right_vel_cmd_);
  return command_interfaces;
}

// ---------------------------------------------------------------------------
// Вспомогательные методы протокола энкодера
// ---------------------------------------------------------------------------

hardware_interface::return_type GuideRobotSystem::read(
  const rclcpp::Time &, const rclcpp::Duration & period)
{
  if (serial_fd_ < 0) {
    return hardware_interface::return_type::OK;
  }

  // 1. Отправляем запрос только раз в 5 циклов (≈10 Hz при update_rate=50 Hz).
  //    Пакет: ff ff 2e 04 03 a0 05 25
  if (++enc_request_counter_ >= 5) {
    enc_request_counter_ = 0;
    uint8_t req[8] = {0xFF, 0xFF, static_cast<uint8_t>(left_wheel_id_), 0x04, 0x03, 0xA0,
                      0x05, 0x25};
    ::write(serial_fd_, req, sizeof(req));
    // Wait for response: at 115200 baud 8+19 bytes ~ 2.4ms, 5ms margin
    usleep(5000);
  }

  // 2. Мгновенно выгребаем из порта все доступные байты (неблокирующее чтение, без usleep!)
  uint8_t buf[256];
  ssize_t n = ::read(serial_fd_, buf, sizeof(buf));
  if (n > 0) {
    for (ssize_t i = 0; i < n; ++i) {
      rx_buffer_.push_back(buf[i]);
    }
  }

  // 3. Сканируем скопившиеся байты на валидные 19-байтные ответы
  while (rx_buffer_.size() >= 19) {
    if (
      rx_buffer_[0] == 0xFF && rx_buffer_[1] == 0xFF &&
      rx_buffer_[2] == static_cast<uint8_t>(left_wheel_id_) && rx_buffer_[3] == 0x0F) {
      // Валидация контрольной суммы: ~sum(b[2..17]) & 0xFF == b[18]
      uint8_t chk_calc = 0;
      for (size_t i = 2; i < 18; ++i) chk_calc += rx_buffer_[i];
      chk_calc = ~chk_calc;

      if (chk_calc == rx_buffer_[18]) {
        // Контрольная сумма верна!
        // ВНИМАНИЕ: при тестировании выявлено что протокол возвращает данные в порядке:
        //   buf[6..9]   = данные ПРАВОГО физического колеса (в ROS — левое после знакового
        //   преобразования) buf[10..13] = данные ЛЕВОГО  физического колеса buf[14..17] = aux
        //   (третий энкодер)
        int32_t enc_right_phys = 0;  // в протоколе b[6..9] = правое физическое
        int32_t enc_left_phys = 0;  // в протоколе b[10..13] = левое физическое
        int32_t enc_aux = 0;
        std::memcpy(&enc_right_phys, &rx_buffer_[6], 4);
        std::memcpy(&enc_left_phys, &rx_buffer_[10], 4);
        std::memcpy(&enc_aux, &rx_buffer_[14], 4);

        double dt = period.seconds();
        if (dt <= 0.0) dt = 0.02;

        constexpr double TWO_PI = 2.0 * M_PI;
        double new_left_pos =
          (static_cast<double>(enc_left_phys) / ticks_per_rev_) * TWO_PI * left_sign_;
        double new_right_pos =
          (static_cast<double>(enc_right_phys) / ticks_per_rev_) * TWO_PI * right_sign_;

        if (!initialized_encoders_) {
          left_position_ = new_left_pos;
          right_position_ = new_right_pos;
          left_velocity_ = 0.0;
          right_velocity_ = 0.0;
          initialized_encoders_ = true;
        } else {
          left_velocity_ = (new_left_pos - left_position_) / dt;
          right_velocity_ = (new_right_pos - right_position_) / dt;
          left_position_ = new_left_pos;
          right_position_ = new_right_pos;
        }

        RCLCPP_INFO_THROTTLE(
          rclcpp::get_logger("GuideRobotSystem"), *clock_, 500,
          "[read] L_ticks=%d (pos=%.3f rad, vel=%.3f rad/s) | R_ticks=%d (pos=%.3f rad, vel=%.3f "
          "rad/s)",
          enc_left_phys, left_position_, left_velocity_, enc_right_phys, right_position_,
          right_velocity_);

        rx_buffer_.erase(rx_buffer_.begin(), rx_buffer_.begin() + 19);
        continue;
      }
    }
    // Если первый байт не заголовок пакета — сдвигаемся на 1 байт
    rx_buffer_.erase(rx_buffer_.begin());
  }

  // Защита от переполнения буфера от шума
  if (rx_buffer_.size() > 512) {
    rx_buffer_.clear();
  }

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

  // 5. Отправка + проверка результата
  ssize_t written = ::write(serial_fd_, packet, sizeof(packet));

  // Всегда логируем (раз в 2с) — для диагностики
  RCLCPP_INFO_THROTTLE(
    rclcpp::get_logger("GuideRobotSystem"), *clock_, 2000,
    "[write] L_cmd=%.3f rad/s -> units=%d | R_cmd=%.3f rad/s -> units=%d | fd=%d | sent=%zd/%zu",
    left_vel_cmd_, l_spd, right_vel_cmd_, r_spd, serial_fd_, written, sizeof(packet));

  if (written != static_cast<ssize_t>(sizeof(packet))) {
    // EAGAIN/EWOULDBLOCK — TX-буфер временно заполнен при O_NONBLOCK, не критично
    if (written == -1 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      RCLCPP_DEBUG(
        rclcpp::get_logger("GuideRobotSystem"), "[write] TX буфер занят (EAGAIN), пропускаем цикл");
    } else {
      RCLCPP_WARN_THROTTLE(
        rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
        "[write] Ошибка записи! Ожидалось %zu байт, отправлено %zd (errno=%d)", sizeof(packet),
        written, errno);
    }
  }

  return hardware_interface::return_type::OK;
}

}  // namespace guide_robot_hardware

#include "pluginlib/class_list_macros.hpp"
PLUGINLIB_EXPORT_CLASS(guide_robot_hardware::GuideRobotSystem, hardware_interface::SystemInterface)
