#include "guide_robot_hardware/guide_robot_system.hpp"

#include <fcntl.h>
#include <termios.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstring>

#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "rclcpp/rclcpp.hpp"

namespace guide_robot_hardware {

namespace {

/// Число из URDF → константа termios. 0 = скорость не поддерживается.
speed_t toTermiosBaud(int baud)
{
  switch (baud) {
    case 9600:
      return B9600;
    case 19200:
      return B19200;
    case 38400:
      return B38400;
    case 57600:
      return B57600;
    case 115200:
      return B115200;
    case 230400:
      return B230400;
    case 460800:
      return B460800;
    case 921600:
      return B921600;
    default:
      return 0;
  }
}

int64_t steadyNowNs()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
           std::chrono::steady_clock::now().time_since_epoch())
    .count();
}

/// Контрольная сумма протокола Future Robot: ~sum(body) & 0xFF.
uint8_t protocolChecksum(const uint8_t * body, size_t len)
{
  uint8_t sum = 0;
  for (size_t i = 0; i < len; ++i) sum += body[i];
  return static_cast<uint8_t>(~sum);
}

}  // namespace

GuideRobotSystem::~GuideRobotSystem()
{
  // Последний рубеж: при SIGINT controller_manager не гарантирует прохода
  // deactivate→cleanup, а мотор-драйвер держит последнюю скорость залоченной.
  stopWatchdog();
  if (serial_fd_ >= 0) {
    writeSpeedPacket(0.0, 0.0);
  }
  closePort();
}

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
  if (info_.hardware_parameters.count("swap_drives") > 0) {
    swap_drives_ = std::stoi(info_.hardware_parameters.at("swap_drives")) != 0;
  }
  left_sign_ = std::stod(info_.hardware_parameters.at("left_sign"));
  right_sign_ = std::stod(info_.hardware_parameters.at("right_sign"));
  speed_coefficient_ = std::stod(info_.hardware_parameters.at("speed_coefficient"));
  if (info_.hardware_parameters.count("wheel_radius") > 0) {
    wheel_radius_ = std::stod(info_.hardware_parameters.at("wheel_radius"));
  }
  if (info_.hardware_parameters.count("ticks_per_rev") > 0) {
    ticks_per_rev_ = std::stod(info_.hardware_parameters.at("ticks_per_rev"));
  }
  if (info_.hardware_parameters.count("cmd_timeout") > 0) {
    cmd_timeout_ = std::stod(info_.hardware_parameters.at("cmd_timeout"));
  }
  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"),
    "Параметры: port=%s baud=%d L_id=%d R_id=%d coeff=%.4f r=%.3fm ticks_per_rev=%.1f "
    "cmd_timeout=%.3fs",
    serial_port_.c_str(), baud_rate_, left_wheel_id_, right_wheel_id_, speed_coefficient_,
    wheel_radius_, ticks_per_rev_, cmd_timeout_);

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

  // Настраиваем <baud_rate> 8N1, raw mode
  const speed_t termios_baud = toTermiosBaud(baud_rate_);
  if (termios_baud == 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "baud_rate=%d не поддерживается (9600/19200/38400/57600/115200/230400/460800/921600)",
      baud_rate_);
    close(serial_fd_);
    serial_fd_ = -1;
    return hardware_interface::CallbackReturn::ERROR;
  }

  struct termios tty;
  memset(&tty, 0, sizeof(tty));
  tcgetattr(serial_fd_, &tty);

  cfsetospeed(&tty, termios_baud);
  cfsetispeed(&tty, termios_baud);

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
    rclcpp::get_logger("GuideRobotSystem"), "Serial порт открыт (O_SYNC, VMIN=0, %d бод): %s",
    baud_rate_, serial_port_.c_str());
  return hardware_interface::CallbackReturn::SUCCESS;
}
hardware_interface::CallbackReturn GuideRobotSystem::on_activate(const rclcpp_lifecycle::State &)
{
  if (serial_fd_ < 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "Активация без открытого порта — on_configure не выполнялся или упал");
    return hardware_interface::CallbackReturn::ERROR;
  }

  rx_buffer_.clear();
  initialized_encoders_ = false;
  enc_elapsed_ = 0.0;
  enc_request_counter_ = 0;
  left_vel_cmd_ = 0.0;
  right_vel_cmd_ = 0.0;

  startWatchdog();

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "GuideRobotSystem активирован (моторы L=%d R=%d)",
    left_wheel_id_, right_wheel_id_);

  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_deactivate(const rclcpp_lifecycle::State &)
{
  stopWatchdog();

  left_vel_cmd_ = 0.0;
  right_vel_cmd_ = 0.0;

  // Обнулить переменные мало: драйвер FURO держит последнюю принятую скорость
  // до следующего пакета, поэтому останов надо ОТПРАВИТЬ.
  if (serial_fd_ >= 0) {
    if (!writeSpeedPacket(0.0, 0.0)) {
      RCLCPP_ERROR(
        rclcpp::get_logger("GuideRobotSystem"),
        "Не удалось отправить стоп-пакет при деактивации — моторы могут продолжать движение!");
    }
    tcdrain(serial_fd_);
  }

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "GuideRobotSystem деактивирован, моторы остановлены");
  return hardware_interface::CallbackReturn::SUCCESS;
}

hardware_interface::CallbackReturn GuideRobotSystem::on_cleanup(const rclcpp_lifecycle::State &)
{
  stopWatchdog();
  closePort();
  return hardware_interface::CallbackReturn::SUCCESS;
}

void GuideRobotSystem::closePort()
{
  std::lock_guard<std::mutex> lock(serial_mutex_);
  if (serial_fd_ < 0) {
    return;
  }
  tcdrain(serial_fd_);
  close(serial_fd_);
  serial_fd_ = -1;
  RCLCPP_INFO(rclcpp::get_logger("GuideRobotSystem"), "Serial порт закрыт");
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

void GuideRobotSystem::sendEncoderRequest()
{
  // Тело пакета (всё, кроме двух стартовых 0xFF и самой контрольной суммы).
  // ID берётся из параметра, поэтому и сумма обязана считаться, а не быть
  // константой: для id=46 она равна 0x25, для любого другого — уже нет,
  // и драйвер молча отбрасывает кадр.
  uint8_t body[5] = {static_cast<uint8_t>(left_wheel_id_), 0x04, 0x03, 0xA0, 0x05};
  uint8_t req[8] = {0xFF,    0xFF,    body[0], body[1],
                    body[2], body[3], body[4], protocolChecksum(body, sizeof(body))};

  std::lock_guard<std::mutex> lock(serial_mutex_);
  if (serial_fd_ < 0) {
    return;
  }
  const ssize_t written = ::write(serial_fd_, req, sizeof(req));
  if (written != static_cast<ssize_t>(sizeof(req))) {
    RCLCPP_WARN_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[read] Запрос энкодеров ушёл не полностью: %zd/%zu (errno=%d)", written, sizeof(req), errno);
  }
}

hardware_interface::return_type GuideRobotSystem::read(
  const rclcpp::Time &, const rclcpp::Duration & period)
{
  if (serial_fd_ < 0) {
    return hardware_interface::return_type::OK;
  }

  enc_elapsed_ += period.seconds();

  // 1. Отправляем запрос только раз в 5 циклов (≈10 Hz при update_rate=50 Hz).
  if (++enc_request_counter_ >= 5) {
    enc_request_counter_ = 0;
    sendEncoderRequest();
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
        // Ответ адресуется теми же ПОЗИЦИОННЫМИ слотами, что и командный пакет
        // в write(): b[6..9] = слот 1, b[10..13] = слот 2, b[14..17] = aux
        // (третий энкодер).
        //
        // Обратная связь ОБЯЗАНА зеркалить write(): там слот 1 всегда
        // масштабируется left_sign_, слот 2 — right_sign_, а swap_drives_
        // решает, КАКОЙ сустав ROS попадает в какой слот. Раньше read() знака
        // swap_drives_ не знал и вешал left_sign_/right_sign_ на слоты
        // наоборот — оба колеса возвращали скорость с обратным знаком.
        // В дифдрайве это переворачивает и v = R/2*(wL+wR), и w = R/W*(wR-wL),
        // из-за чего одометрия ехала назад при движении вперёд и вращалась
        // вправо при повороте влево.
        int32_t enc_slot1 = 0;
        int32_t enc_slot2 = 0;
        int32_t enc_aux = 0;
        std::memcpy(&enc_slot1, &rx_buffer_[6], 4);
        std::memcpy(&enc_slot2, &rx_buffer_[10], 4);
        std::memcpy(&enc_aux, &rx_buffer_[14], 4);

        // dt — время с ПРОШЛОГО разобранного кадра, а не длительность цикла:
        // запрос уходит раз в 5 циклов, поэтому дельта позиции набегает
        // примерно за 5 * period. Деление на period завышало скорость впятеро.
        double dt = enc_elapsed_;
        if (dt < 1e-6) dt = period.seconds();  // два кадра в одном цикле
        if (dt <= 0.0) dt = 0.02;
        enc_elapsed_ = 0.0;

        constexpr double TWO_PI = 2.0 * M_PI;
        double slot1_pos = (static_cast<double>(enc_slot1) / ticks_per_rev_) * TWO_PI * left_sign_;
        double slot2_pos = (static_cast<double>(enc_slot2) / ticks_per_rev_) * TWO_PI * right_sign_;

        double new_left_pos = swap_drives_ ? slot2_pos : slot1_pos;
        double new_right_pos = swap_drives_ ? slot1_pos : slot2_pos;

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

        RCLCPP_DEBUG_THROTTLE(
          rclcpp::get_logger("GuideRobotSystem"), *clock_, 500,
          "[read] slot1_ticks=%d slot2_ticks=%d | L pos=%.3f rad vel=%.3f rad/s | R pos=%.3f rad "
          "vel=%.3f rad/s",
          enc_slot1, enc_slot2, left_position_, left_velocity_, right_position_, right_velocity_);

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

int16_t GuideRobotSystem::toMotorUnits(double omega, double sign) const
{
  // v (м/с) = omega (рад/с) * wheel_radius; units = v / speed_coefficient
  double v = omega * wheel_radius_;
  double units = sign * v / speed_coefficient_;
  // Ограничиваем диапазон int16
  if (units > 32767) units = 32767;
  if (units < -32768) units = -32768;
  return static_cast<int16_t>(units);
}

bool GuideRobotSystem::writeSpeedPacket(double slot1_cmd, double slot2_cmd)
{
  int16_t l_spd = toMotorUnits(slot1_cmd, left_sign_);
  int16_t r_spd = toMotorUnits(slot2_cmd, right_sign_);

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

  // 3. Итоговый пакет: [0xFF, 0xFF] + body + [checksum]
  uint8_t packet[18];
  packet[0] = 0xFF;
  packet[1] = 0xFF;
  std::memcpy(&packet[2], body, sizeof(body));
  packet[17] = protocolChecksum(body, sizeof(body));

  // 4. Отправка + проверка результата
  std::lock_guard<std::mutex> lock(serial_mutex_);
  if (serial_fd_ < 0) {
    return false;
  }
  const ssize_t written = ::write(serial_fd_, packet, sizeof(packet));

  // Диагностика: включается через --ros-args --log-level GuideRobotSystem:=debug
  RCLCPP_DEBUG_THROTTLE(
    rclcpp::get_logger("GuideRobotSystem"), *clock_, 2000,
    "[write] slot1=%.3f rad/s -> units=%d | slot2=%.3f rad/s -> units=%d | fd=%d | sent=%zd/%zu",
    slot1_cmd, l_spd, slot2_cmd, r_spd, serial_fd_, written, sizeof(packet));

  if (written != static_cast<ssize_t>(sizeof(packet))) {
    RCLCPP_WARN_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[write] Ошибка записи! Ожидалось %zu байт, отправлено %zd (errno=%d)", sizeof(packet),
      written, errno);
    return false;
  }
  return true;
}

hardware_interface::return_type GuideRobotSystem::write(
  const rclcpp::Time &, const rclcpp::Duration &)
{
  if (serial_fd_ < 0) {
    return hardware_interface::return_type::OK;  // порт не открыт — молчим
  }

  // Слоты пакета адресуются по ПОЗИЦИИ (проверено на железе: смена ID байта
  // в слоте эффекта не даёт). left_sign_/right_sign_ компенсируют зеркальную
  // установку мотора В КОНКРЕТНОМ слоте (см. работающую езду прямо), поэтому
  // при swap_drives меняем местами именно ИСТОЧНИК команды, а не готовые
  // знаковые значения — иначе компенсация знака съезжает не на тот мотор.
  const double slot1_cmd = swap_drives_ ? right_vel_cmd_ : left_vel_cmd_;
  const double slot2_cmd = swap_drives_ ? left_vel_cmd_ : right_vel_cmd_;

  writeSpeedPacket(slot1_cmd, slot2_cmd);
  last_write_ns_.store(steadyNowNs());

  return hardware_interface::return_type::OK;
}

// ---------------------------------------------------------------------------
// Сторожевой таймер
// ---------------------------------------------------------------------------

void GuideRobotSystem::startWatchdog()
{
  if (cmd_timeout_ <= 0.0) {
    RCLCPP_WARN(
      rclcpp::get_logger("GuideRobotSystem"),
      "cmd_timeout не задан — сторожевой таймер выключен, при зависании управляющего "
      "цикла моторы продолжат движение");
    return;
  }
  last_write_ns_.store(steadyNowNs());
  watchdog_running_.store(true);
  watchdog_thread_ = std::thread(&GuideRobotSystem::watchdogLoop, this);
  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Сторожевой таймер запущен (cmd_timeout=%.3f с)",
    cmd_timeout_);
}

void GuideRobotSystem::stopWatchdog()
{
  watchdog_running_.store(false);
  if (watchdog_thread_.joinable()) {
    watchdog_thread_.join();
  }
}

void GuideRobotSystem::watchdogLoop()
{
  // Проверяем чаще таймаута, чтобы реакция была не хуже самого таймаута.
  const auto tick = std::chrono::milliseconds(20);
  bool tripped = false;

  while (watchdog_running_.load()) {
    std::this_thread::sleep_for(tick);

    const double age = static_cast<double>(steadyNowNs() - last_write_ns_.load()) * 1e-9;
    if (age > cmd_timeout_) {
      // Повторяем нули, пока цикл не оживёт: одиночный пакет мог не дойти,
      // а цена ошибки — 62 кг, которые продолжают ехать.
      writeSpeedPacket(0.0, 0.0);
      if (!tripped) {
        tripped = true;
        RCLCPP_ERROR(
          rclcpp::get_logger("GuideRobotSystem"),
          "[watchdog] write() не вызывался %.3f с (> cmd_timeout=%.3f) — моторы остановлены", age,
          cmd_timeout_);
      }
    } else if (tripped) {
      tripped = false;
      RCLCPP_INFO(
        rclcpp::get_logger("GuideRobotSystem"), "[watchdog] управляющий цикл восстановился");
    }
  }
}

}  // namespace guide_robot_hardware

#include "pluginlib/class_list_macros.hpp"
PLUGINLIB_EXPORT_CLASS(guide_robot_hardware::GuideRobotSystem, hardware_interface::SystemInterface)
