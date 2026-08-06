#include "guide_robot_hardware/guide_robot_system.hpp"

#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <unordered_map>

#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "rclcpp/rclcpp.hpp"

namespace guide_robot_hardware {

namespace {

using HwParams = std::unordered_map<std::string, std::string>;

/// Бюджет ожидания ответа энкодеров, мс. При 115200 бод запрос (8 байт) +
/// ответ (19 байт) занимают на шине ~2.4 мс, остальное — запас на задержку
/// драйвера. Реально poll() возвращается раньше, как только кадр пришёл.
constexpr int ENC_RESPONSE_BUDGET_MS = 5;

/// Длина ответа энкодеров.
constexpr size_t ENC_FRAME_LEN = 19;

std::string trim(const std::string & s)
{
  const size_t b = s.find_first_not_of(" \t\r\n");
  if (b == std::string::npos) return {};
  return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}

/// Общая часть разбора: найти параметр и вернуть его очищенное значение.
/// nullptr — параметра нет (для обязательного уже залогирован ERROR).
const std::string * findParam(const HwParams & params, const char * name, bool required)
{
  const auto it = params.find(name);
  if (it == params.end()) {
    if (required) {
      RCLCPP_ERROR(
        rclcpp::get_logger("GuideRobotSystem"),
        "Обязательный параметр '%s' не задан в <hardware> — проверь "
        "guide_robot_description/urdf/guide_robot.ros2_control.xacro",
        name);
    }
    return nullptr;
  }
  return &it->second;
}

/// Разбор числового параметра с указанием ВИНОВНИКА в сообщении: голое
/// исключение из std::stod наружу не говорит, какой параметр сломан.
/// false — только ошибка; отсутствие необязательного параметра out не трогает.
bool paramDouble(const HwParams & params, const char * name, bool required, double & out)
{
  const std::string * raw = findParam(params, name, required);
  if (raw == nullptr) return !required;
  const std::string value = trim(*raw);
  try {
    size_t pos = 0;
    const double parsed = std::stod(value, &pos);
    if (pos != value.size()) {
      throw std::invalid_argument("лишние символы после числа");
    }
    if (!std::isfinite(parsed)) {
      throw std::invalid_argument("не конечное число");
    }
    out = parsed;
  } catch (const std::exception & e) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "Параметр '%s' = '%s' не разбирается как число: %s",
      name, value.c_str(), e.what());
    return false;
  }
  return true;
}

bool paramInt(const HwParams & params, const char * name, bool required, int & out)
{
  double value = out;
  if (!paramDouble(params, name, required, value)) return false;
  out = static_cast<int>(value);
  return true;
}

bool paramString(const HwParams & params, const char * name, bool required, std::string & out)
{
  const std::string * raw = findParam(params, name, required);
  if (raw == nullptr) return !required;
  out = trim(*raw);
  if (out.empty()) {
    RCLCPP_ERROR(rclcpp::get_logger("GuideRobotSystem"), "Параметр '%s' пуст", name);
    return false;
  }
  return true;
}

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
  if (!loadParameters()) {
    return hardware_interface::CallbackReturn::ERROR;
  }

  clock_ = std::make_shared<rclcpp::Clock>(RCL_STEADY_TIME);

  return hardware_interface::CallbackReturn::SUCCESS;
}

bool GuideRobotSystem::loadParameters()
{
  const HwParams & p = info_.hardware_parameters;

  // Обязательные: без любого из них компонент не имеет права крутить моторы.
  // cmd_timeout и max_wheel_velocity — тоже обязательные, и это осознанно:
  // это две единственные защиты уровня плагина, и «забыли передать» не должно
  // молча означать «защиты нет».
  bool ok = true;
  ok &= paramString(p, "serial_port", true, serial_port_);
  ok &= paramInt(p, "baud_rate", true, baud_rate_);
  ok &= paramInt(p, "left_wheel_id", true, left_wheel_id_);
  ok &= paramInt(p, "right_wheel_id", true, right_wheel_id_);
  ok &= paramDouble(p, "left_sign", true, left_sign_);
  ok &= paramDouble(p, "right_sign", true, right_sign_);
  ok &= paramDouble(p, "speed_coefficient", true, speed_coefficient_);
  ok &= paramDouble(p, "cmd_timeout", true, cmd_timeout_);
  ok &= paramDouble(p, "max_wheel_velocity", true, max_wheel_velocity_);

  // Необязательные: дефолты в hpp соответствуют текущему железу.
  int swap = swap_drives_ ? 1 : 0;
  ok &= paramInt(p, "swap_drives", false, swap);
  swap_drives_ = (swap != 0);
  ok &= paramDouble(p, "wheel_radius", false, wheel_radius_);
  ok &= paramDouble(p, "ticks_per_rev", false, ticks_per_rev_);
  ok &= paramDouble(p, "encoder_timeout", false, encoder_timeout_);
  ok &= paramDouble(p, "encoder_wrap_ticks", false, encoder_wrap_ticks_);
  ok &= paramInt(p, "encoder_poll_divider", false, encoder_poll_divider_);

  double accel = static_cast<double>(motor_accel_);
  ok &= paramDouble(p, "motor_accel", false, accel);

  if (!ok) {
    return false;
  }

  // Диапазоны: 0 или отрицательное здесь — не «выключено», а опечатка,
  // которая тихо испортит либо кинематику, либо защиту.
  if (cmd_timeout_ <= 0.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "cmd_timeout=%.3f: сторожевой таймер обязателен, значение должно быть > 0", cmd_timeout_);
    return false;
  }
  if (max_wheel_velocity_ <= 0.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "max_wheel_velocity=%.3f: должно быть > 0 (рад/с)",
      max_wheel_velocity_);
    return false;
  }
  if (speed_coefficient_ <= 0.0 || wheel_radius_ <= 0.0 || ticks_per_rev_ <= 0.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "speed_coefficient=%.6f, wheel_radius=%.3f, ticks_per_rev=%.1f: все должны быть > 0",
      speed_coefficient_, wheel_radius_, ticks_per_rev_);
    return false;
  }
  if (accel < 0.0 || accel > 65535.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "motor_accel=%.0f вне диапазона uint16", accel);
    return false;
  }
  motor_accel_ = static_cast<uint16_t>(accel);

  if (encoder_poll_divider_ < 1) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "encoder_poll_divider=%d: должно быть >= 1 (1 — запрос каждый цикл)", encoder_poll_divider_);
    return false;
  }
  if (encoder_wrap_ticks_ < 0.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "encoder_wrap_ticks=%.0f: должно быть >= 0 (0 выключает ремонт разрывов)",
      encoder_wrap_ticks_);
    return false;
  }

  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"),
    "Параметры: port=%s baud=%d L_id=%d R_id=%d swap=%d coeff=%.6f r=%.3fm ticks_per_rev=%.1f "
    "cmd_timeout=%.3fs max_wheel_vel=%.2f рад/с encoder_timeout=%.3fs accel=%u "
    "poll_divider=%d wrap_ticks=%.0f",
    serial_port_.c_str(), baud_rate_, left_wheel_id_, right_wheel_id_, swap_drives_ ? 1 : 0,
    speed_coefficient_, wheel_radius_, ticks_per_rev_, cmd_timeout_, max_wheel_velocity_,
    encoder_timeout_, motor_accel_, encoder_poll_divider_, encoder_wrap_ticks_);

  if (encoder_timeout_ <= 0.0) {
    RCLCPP_WARN(
      rclcpp::get_logger("GuideRobotSystem"),
      "encoder_timeout <= 0: отказ энкодеров НЕ будет обнаружен, одометрия замрёт молча");
  }
  if (encoder_wrap_ticks_ <= 0.0) {
    RCLCPP_WARN(
      rclcpp::get_logger("GuideRobotSystem"),
      "encoder_wrap_ticks = 0: разрывы счётчика драйвера НЕ чинятся. Наблюдавшийся "
      "на роботе скачок на 2^16 тиков уедет в одометрию как настоящий поворот");
  }

  return true;
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
  if (tcgetattr(serial_fd_, &tty) != 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "tcgetattr(%s) не удался (errno=%d: %s)",
      serial_port_.c_str(), errno, strerror(errno));
    close(serial_fd_);
    serial_fd_ = -1;
    return hardware_interface::CallbackReturn::ERROR;
  }

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

  if (tcsetattr(serial_fd_, TCSANOW, &tty) != 0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"), "tcsetattr(%s) не удался (errno=%d: %s)",
      serial_port_.c_str(), errno, strerror(errno));
    close(serial_fd_);
    serial_fd_ = -1;
    return hardware_interface::CallbackReturn::ERROR;
  }

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
  enc_silence_ = 0.0;
  enc_fault_ = false;
  enc_request_counter_ = 0;
  prev_raw_slot1_ = 0;
  prev_raw_slot2_ = 0;
  unwrapped_slot1_ = 0;
  unwrapped_slot2_ = 0;
  prev_slot1_rate_ = 0.0;
  prev_slot2_rate_ = 0.0;
  enc_wrap_repairs_ = 0;
  enc_frames_rejected_ = 0;
  left_vel_cmd_ = 0.0;
  right_vel_cmd_ = 0.0;

  // Без сторожа активироваться нельзя: у драйвера FURO нет своего таймаута
  // команд, и зависший управляющий цикл означает 62 кг, едущие с последней
  // принятой скоростью до упора. Раньше здесь был WARN и SUCCESS.
  if (!startWatchdog()) {
    return hardware_interface::CallbackReturn::ERROR;
  }

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

void GuideRobotSystem::sendEncoderRequestLocked()
{
  // Тело пакета (всё, кроме двух стартовых 0xFF и самой контрольной суммы).
  // ID берётся из параметра, поэтому и сумма обязана считаться, а не быть
  // константой: для id=46 она равна 0x25, для любого другого — уже нет,
  // и драйвер молча отбрасывает кадр.
  uint8_t body[5] = {static_cast<uint8_t>(left_wheel_id_), 0x04, 0x03, 0xA0, 0x05};
  uint8_t req[8] = {0xFF,    0xFF,    body[0], body[1],
                    body[2], body[3], body[4], protocolChecksum(body, sizeof(body))};

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

void GuideRobotSystem::drainPortLocked(int budget_ms, size_t expect_bytes)
{
  if (serial_fd_ < 0) {
    return;
  }

  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(budget_ms);
  size_t got = 0;
  uint8_t buf[256];

  for (;;) {
    // VMIN=0/VTIME=0 в termios: ::read возвращается немедленно с тем, что есть.
    const ssize_t n = ::read(serial_fd_, buf, sizeof(buf));
    if (n > 0) {
      rx_buffer_.insert(rx_buffer_.end(), buf, buf + n);
      got += static_cast<size_t>(n);
    } else if (n < 0 && errno != EAGAIN && errno != EINTR) {
      RCLCPP_WARN_THROTTLE(
        rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
        "[read] Ошибка чтения из порта (errno=%d: %s)", errno, strerror(errno));
      return;
    }

    // budget_ms == 0 — цикл без запроса: просто забрать накопившееся и уйти,
    // ни миллисекунды RT-времени на ожидание.
    if (budget_ms <= 0 || got >= expect_bytes) {
      return;
    }

    const auto now = std::chrono::steady_clock::now();
    if (now >= deadline) {
      return;
    }
    const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
    struct pollfd pfd;
    pfd.fd = serial_fd_;
    pfd.events = POLLIN;
    pfd.revents = 0;
    const int rc = ::poll(&pfd, 1, static_cast<int>(left.count()) + 1);
    if (rc < 0) {
      if (errno == EINTR) continue;
      RCLCPP_WARN_THROTTLE(
        rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000, "[read] poll() упал (errno=%d: %s)",
        errno, strerror(errno));
      return;
    }
    if (rc == 0) {
      return;  // бюджет исчерпан, ответа нет — молча, разбирается детектором тишины
    }
    if ((pfd.revents & POLLIN) == 0) {
      // POLLERR/POLLHUP/POLLNVAL без данных: крутиться до дедлайна бессмысленно.
      RCLCPP_WARN_THROTTLE(
        rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000, "[read] poll() revents=0x%x",
        pfd.revents);
      return;
    }
  }
}

bool GuideRobotSystem::correctTickDelta(
  int64_t raw_delta, double dt, double prev_rate, const char * slot_name, int64_t & out_delta)
{
  // Конверт правдоподобия: сколько тиков колесо физически способно набрать за
  // dt. max_wheel_velocity_ — уже конверт худшего случая v+w с запасом 1.10
  // (w_wheel_max в xacro), поэтому второго множителя здесь нет.
  const double envelope = max_wheel_velocity_ * dt * ticks_per_rev_ / (2.0 * M_PI);

  // Пока дельта меньше полупериода счётчика, разрыв и честный ход различимы по
  // одному кадру. Дальше они дают одно и то же показание — информационный
  // предел, а не выбор реализации.
  double unambiguous = envelope;
  if (encoder_wrap_ticks_ > 0.0) {
    unambiguous = std::min(envelope, encoder_wrap_ticks_ * 0.5);
  }

  const double raw_abs = std::fabs(static_cast<double>(raw_delta));
  if (raw_abs <= unambiguous) {
    out_delta = raw_delta;  // штатный кадр — не трогаем
    return true;
  }

  if (encoder_wrap_ticks_ <= 0.0) {
    RCLCPP_ERROR_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[encoders] %s: дельта %+ld тиков за %.3f с невозможна (конверт %.0f), ремонт "
      "разрывов выключен (encoder_wrap_ticks=0) — кадр отвергнут",
      slot_name, static_cast<long>(raw_delta), dt, envelope);
    return false;
  }

  // Кандидаты проверяются по физическому конверту, а НЕ по полупериоду: кадры
  // приходят раз в ~215 мс, за это время колесо на 0.9 м/с набирает 39 300
  // тиков — больше полупериода 32 768. Проверка по полупериоду отвергала бы
  // каждый кадр прямой на полном ходу и валила бы энкодер-каскад в отказ.
  const int64_t wrap = static_cast<int64_t>(encoder_wrap_ticks_);
  const int64_t cand[3] = {raw_delta, raw_delta - wrap, raw_delta + wrap};
  bool ok[3];
  int n_ok = 0;
  for (int i = 0; i < 3; ++i) {
    ok[i] = std::fabs(static_cast<double>(cand[i])) <= envelope;
    n_ok += ok[i] ? 1 : 0;
  }

  if (n_ok == 0) {
    RCLCPP_ERROR_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[encoders] %s: дельта %+ld тиков за %.3f с невозможна (конверт %.0f) и не "
      "объясняется разрывом на %ld — кадр отвергнут",
      slot_name, static_cast<long>(raw_delta), dt, envelope, static_cast<long>(wrap));
    return false;
  }

  // Несколько достижимых кандидатов по одному кадру не различить — разрешаем
  // непрерывностью: разрыв означал бы скачок скорости колеса на ~15 рад/с,
  // чего 62-килограммовая база не разгонит и не затормозит. При n_ok == 1
  // цикл просто берёт единственного.
  int best = -1;
  double best_err = 0.0;
  for (int i = 0; i < 3; ++i) {
    if (!ok[i]) continue;
    const double err = std::fabs(static_cast<double>(cand[i]) / dt - prev_rate);
    if (best < 0 || err < best_err) {
      best = i;
      best_err = err;
    }
  }

  out_delta = cand[best];

  if (best == 0) {
    // Дельта за полупериодом, но физически достижима и согласуется с прошлой
    // скоростью: верить драйверу безопаснее, чем тормозить робота на ходу.
    // Цена — разрыв, случившийся именно в этой полосе, будет пропущен.
    RCLCPP_WARN_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 5000,
      "[encoders] %s: дельта %+ld тиков за %.3f с больше полупериода счётчика (%.0f), но в "
      "физическом конверте (%.0f) и согласуется с прошлой скоростью — принята как есть",
      slot_name, static_cast<long>(raw_delta), dt, unambiguous, envelope);
    return true;
  }

  // Сообщаем величину самого разрыва, а не поправки: знак у неё обратный.
  const int64_t glitch = raw_delta - cand[best];
  ++enc_wrap_repairs_;
  RCLCPP_WARN(
    rclcpp::get_logger("GuideRobotSystem"),
    "[encoders] %s: разрыв счётчика драйвера на %+ld тиков (%+.3f рад на колесе) починен "
    "разворачиванием; сырая дельта %+ld за %.3f с при конверте %.0f. Это дефект прошивки "
    "FURO, не наш расчёт. Всего разрывов с активации: %lu",
    slot_name, static_cast<long>(glitch), static_cast<double>(glitch) / ticks_per_rev_ * 2.0 * M_PI,
    static_cast<long>(raw_delta), dt, envelope, static_cast<unsigned long>(enc_wrap_repairs_));
  return true;
}

hardware_interface::return_type GuideRobotSystem::read(
  const rclcpp::Time &, const rclcpp::Duration & period)
{
  if (serial_fd_ < 0) {
    return hardware_interface::return_type::OK;
  }

  enc_elapsed_ += period.seconds();
  enc_silence_ += period.seconds();

  // 1. Обмен с шиной — под мьютексом целиком: RS-485 полудуплексный, и
  //    стоп-пакет сторожа, ушедший поперёк окна приёма, испортил бы ответ.
  //    Запрос уходит раз в encoder_poll_divider_ циклов; ожидание — через
  //    poll() с бюджетом, а не фиксированным usleep(5000), который отъедал
  //    четверть цикла даже когда кадр уже лежал в буфере.
  {
    std::lock_guard<std::mutex> lock(serial_mutex_);
    if (++enc_request_counter_ >= encoder_poll_divider_) {
      enc_request_counter_ = 0;
      sendEncoderRequestLocked();
      drainPortLocked(ENC_RESPONSE_BUDGET_MS, ENC_FRAME_LEN);
    } else {
      drainPortLocked(0, 0);
    }
  }

  // 2. Сканируем скопившиеся байты на валидные 19-байтные ответы
  while (rx_buffer_.size() >= ENC_FRAME_LEN) {
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
        // при encoder_poll_divider_ > 1 дельта позиции набегает за несколько
        // периодов. Деление на period завышало скорость во столько же раз.
        // НЕ обнуляем здесь: отвергнутый кадр не должен сбрасывать накопленное
        // время, иначе следующий годный кадр поделит дельту на слишком малый dt.
        double dt = enc_elapsed_;
        if (dt < 1e-6) dt = period.seconds();  // два кадра в одном цикле
        if (dt <= 0.0) dt = 0.02;

        if (!initialized_encoders_) {
          // Первый кадр после активации задаёт начало отсчёта: сравнивать
          // не с чем, ремонт разрывов неприменим.
          prev_raw_slot1_ = enc_slot1;
          prev_raw_slot2_ = enc_slot2;
          unwrapped_slot1_ = enc_slot1;
          unwrapped_slot2_ = enc_slot2;
          prev_slot1_rate_ = 0.0;
          prev_slot2_rate_ = 0.0;
        } else {
          // Дельта считается в int64: разность двух int32 переполняет int32.
          const int64_t raw_d1 = static_cast<int64_t>(enc_slot1) - prev_raw_slot1_;
          const int64_t raw_d2 = static_cast<int64_t>(enc_slot2) - prev_raw_slot2_;

          int64_t d1 = 0;
          int64_t d2 = 0;
          // Оба слота считаем ДО применения: кадр принимается целиком или
          // не принимается вовсе — полкадра в одометрии хуже, чем ничего.
          if (
            !correctTickDelta(raw_d1, dt, prev_slot1_rate_, "слот 1", d1) ||
            !correctTickDelta(raw_d2, dt, prev_slot2_rate_, "слот 2", d2)) {
            ++enc_frames_rejected_;
            // Позиции, enc_elapsed_ и enc_silence_ не трогаем: для верхнего
            // стека это «кадра не было». Подряд идущие отказы штатно доводят
            // до детектора тишины, и это правильный исход.
            rx_buffer_.erase(rx_buffer_.begin(), rx_buffer_.begin() + ENC_FRAME_LEN);
            continue;
          }

          prev_raw_slot1_ = enc_slot1;
          prev_raw_slot2_ = enc_slot2;
          unwrapped_slot1_ += d1;
          unwrapped_slot2_ += d2;
          // Опора для следующего кадра — по ПРИНЯТОЙ дельте, а не по сырой:
          // сырая после разрыва указывала бы на скорость, которой не было.
          prev_slot1_rate_ = static_cast<double>(d1) / dt;
          prev_slot2_rate_ = static_cast<double>(d2) / dt;
        }

        enc_elapsed_ = 0.0;

        // Единственное место, где тишина энкодеров считается прерванной.
        enc_silence_ = 0.0;
        if (enc_fault_) {
          enc_fault_ = false;
          RCLCPP_INFO(
            rclcpp::get_logger("GuideRobotSystem"), "[encoders] связь с энкодерами восстановлена");
        }

        // Позиция — из развёрнутого счёта, а не из сырого: сырое после
        // починенного разрыва остаётся смещённым на 2^16 навсегда.
        constexpr double TWO_PI = 2.0 * M_PI;
        double slot1_pos =
          (static_cast<double>(unwrapped_slot1_) / ticks_per_rev_) * TWO_PI * left_sign_;
        double slot2_pos =
          (static_cast<double>(unwrapped_slot2_) / ticks_per_rev_) * TWO_PI * right_sign_;

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

        rx_buffer_.erase(rx_buffer_.begin(), rx_buffer_.begin() + ENC_FRAME_LEN);
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

  // 3. Детектор отказа энкодер-каскада.
  //    write() не зависит от состояния энкодеров и продолжал бы гнать команды
  //    открытым циклом, а /joint_states — стоять на месте. Для одометрии, EKF
  //    и Nav2 это не «нет данных», а «робот стоит»: ошибка молча уезжает вверх
  //    по стеку. Поэтому здесь ERROR, по которому controller_manager снимет
  //    компонент с активации.
  //
  //    Стоп-пакет шлём САМИ и сразу: ERROR из read() ведёт компонент через
  //    on_error, а не гарантированно через on_deactivate, поэтому штатный
  //    останов оттуда может не случиться, а драйвер FURO держит последнюю
  //    принятую скорость до следующего пакета.
  if (encoder_timeout_ > 0.0 && enc_silence_ > encoder_timeout_) {
    if (!enc_fault_) {
      enc_fault_ = true;
      RCLCPP_ERROR(
        rclcpp::get_logger("GuideRobotSystem"),
        "[encoders] нет валидных кадров %.3f с (> encoder_timeout=%.3f)%s — компонент в ошибку, "
        "движение прекращается. Отвергнуто кадров с активации: %lu, починено разрывов: %lu",
        enc_silence_, encoder_timeout_,
        initialized_encoders_ ? "" : " (ни одного кадра с момента активации)",
        static_cast<unsigned long>(enc_frames_rejected_),
        static_cast<unsigned long>(enc_wrap_repairs_));
    }
    left_vel_cmd_ = 0.0;
    right_vel_cmd_ = 0.0;
    writeSpeedPacket(0.0, 0.0);
    return hardware_interface::return_type::ERROR;
  }

  return hardware_interface::return_type::OK;
}

int16_t GuideRobotSystem::toMotorUnits(double omega, double sign) const
{
  // NaN/inf в команде: static_cast такого в int16_t — UB, а на шине это
  // означает произвольную скорость. Единственный безопасный ответ — стоп.
  if (!std::isfinite(omega)) {
    RCLCPP_ERROR_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[write] Не конечная команда скорости — отправляю 0");
    return 0;
  }

  // Независимый рубеж защиты (defense-in-depth). Штатно команды уже
  // отфильтрованы SpeedLimiter'ом diff_drive_controller, но клэмп по
  // диапазону int16 ниже — это ~5.6 м/с на колесе, то есть не защита вовсе.
  // Если к тем же command interface когда-нибудь подключится контроллер без
  // лимитера (forward_command_controller для отладки), это единственное, что
  // стоит между странной командой и полным ходом 62-килограммовой базы.
  if (omega > max_wheel_velocity_ || omega < -max_wheel_velocity_) {
    RCLCPP_WARN_THROTTLE(
      rclcpp::get_logger("GuideRobotSystem"), *clock_, 1000,
      "[write] Команда %.3f рад/с обрезана по max_wheel_velocity=%.3f рад/с", omega,
      max_wheel_velocity_);
    omega = omega > 0.0 ? max_wheel_velocity_ : -max_wheel_velocity_;
  }

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

  const uint16_t accel = motor_accel_;

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
    static_cast<uint8_t>(accel & 0xFF),
    static_cast<uint8_t>((accel >> 8) & 0xFF),
    static_cast<uint8_t>(right_wheel_id_),
    static_cast<uint8_t>(r_spd & 0xFF),
    static_cast<uint8_t>((r_spd >> 8) & 0xFF),
    static_cast<uint8_t>(accel & 0xFF),
    static_cast<uint8_t>((accel >> 8) & 0xFF),
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

bool GuideRobotSystem::startWatchdog()
{
  // Дублирует проверку из loadParameters(): активация без сторожа недопустима
  // независимо от того, каким путём сюда пришли.
  if (cmd_timeout_ <= 0.0) {
    RCLCPP_ERROR(
      rclcpp::get_logger("GuideRobotSystem"),
      "cmd_timeout=%.3f — сторожевой таймер не может быть запущен, активация отклонена: "
      "при зависании управляющего цикла моторы держали бы последнюю скорость",
      cmd_timeout_);
    return false;
  }
  last_write_ns_.store(steadyNowNs());
  watchdog_running_.store(true);
  watchdog_thread_ = std::thread(&GuideRobotSystem::watchdogLoop, this);
  RCLCPP_INFO(
    rclcpp::get_logger("GuideRobotSystem"), "Сторожевой таймер запущен (cmd_timeout=%.3f с)",
    cmd_timeout_);
  return true;
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
