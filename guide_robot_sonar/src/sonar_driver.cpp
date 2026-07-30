#include "guide_robot_sonar/sonar_driver.hpp"

#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <chrono>
#include <iostream>

namespace guide_robot_sonar {

namespace {

// This timeout caps the distance a sensor is able to report at all. The UART
// costs ~8.3 ms of it (3-byte request + 5-byte reply at 9600 8N1); whatever
// is left is echo flight time, so
//
//     max reportable distance = (timeout_ms - 8.3) / 1000 * 343 / 2
//         60 ms -> ~8.9 m        30 ms -> ~3.7 m
//
// Size it against the sensor's own range, NOT against the node's max_range.
// A sensor looking at something 5 m away answers with a valid 5 m frame ~37 ms
// in; clipping that is not "out of range", it is a missing reply, and the node
// escalates it to a bus fault. 30 ms was tried on hardware for exactly the
// wrong reason (budgeting from max_range = 2 m) and put the two front sensors,
// which faced >3 m of open space, into permanent fault — the fail-safe then
// stopped the robot mid-goal.
constexpr int kResponseTimeoutMs = 60;

// Settle time after a *received* echo, before pinging the next sensor. Indoor
// multipath (wall/floor reflections of the previous ping) can persist past the
// direct-path echo and get misread as the next sensor's reply, which is the
// dominant source of the 10-15 cm cross-sensor jitter.
constexpr int kSettleMs = 30;

// Used only when the request never went out (dead fd, unplugged adapter).
// Nothing was pinged, so there is no echo to wait out — but without a floor
// the round-robin would spin on a failing write() hundreds of times a second.
constexpr int kErrorBackoffMs = 5;

// query_sonar() result for "the request could not be sent at all", as opposed
// to -1 which means the request went out and the answer was missing or
// malformed. Callers treat both as errors; only the settle time differs.
constexpr int kBusUnavailable = -2;

}  // namespace

SonarDriver::SonarDriver(const std::string & port, int baudrate) : port_(port), baudrate_(baudrate)
{
  const auto now = std::chrono::steady_clock::now();
  for (int id : sonar_ids_) {
    SonarReading reading;
    reading.stamp = now;
    readings_[id] = reading;
  }
}

SonarDriver::~SonarDriver()
{
  stop();
}

void SonarDriver::set_info_logger(LogFn fn)
{
  std::lock_guard<std::mutex> lock(log_mutex_);
  info_logger_ = std::move(fn);
}

void SonarDriver::set_error_logger(LogFn fn)
{
  std::lock_guard<std::mutex> lock(log_mutex_);
  error_logger_ = std::move(fn);
}

void SonarDriver::log_info(const std::string & msg)
{
  std::lock_guard<std::mutex> lock(log_mutex_);
  if (info_logger_) {
    info_logger_(msg);
  } else {
    std::cout << msg << std::endl;
  }
}

void SonarDriver::log_error(const std::string & msg)
{
  std::lock_guard<std::mutex> lock(log_mutex_);
  if (error_logger_) {
    error_logger_(msg);
  } else {
    std::cerr << msg << std::endl;
  }
}

bool SonarDriver::is_dome_active() const
{
  return dome_active_;
}

void SonarDriver::start()
{
  if (running_) {
    return;
  }

  if (!open_port()) {
    log_error("Failed to open serial port: " + port_);
    return;
  }

  log_info("Opened serial port: " + port_);

  activate_dome();

  // Age is measured from here, so the first poll round is not already stale
  // when the consumer looks at it.
  {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto now = std::chrono::steady_clock::now();
    for (auto & entry : readings_) {
      entry.second.stamp = now;
    }
  }

  running_ = true;
  thread_ = std::thread(&SonarDriver::run_loop, this);
}

void SonarDriver::stop()
{
  running_ = false;
  if (thread_.joinable()) {
    thread_.join();
  }
  close_port();
}

std::map<int, SonarReading> SonarDriver::get_readings()
{
  const auto now = std::chrono::steady_clock::now();
  std::lock_guard<std::mutex> lock(mutex_);
  std::map<int, SonarReading> snapshot = readings_;
  for (auto & entry : snapshot) {
    entry.second.age_s = std::chrono::duration<double>(now - entry.second.stamp).count();
  }
  return snapshot;
}

bool SonarDriver::open_port()
{
  serial_fd_ = open(port_.c_str(), O_RDWR | O_NOCTTY | O_NDELAY);
  if (serial_fd_ < 0) {
    return false;
  }

  // Exclusive access: a second process opening the same port (e.g. two
  // launch files pointing at /dev/tty_sonar at once) gets EBUSY instead of
  // silently interleaving reads/writes on the shared UART.
  if (ioctl(serial_fd_, TIOCEXCL) != 0) {
    log_error(
      "Failed to set exclusive access (TIOCEXCL) on " + port_ +
      " — another process may already hold this port");
    close(serial_fd_);
    serial_fd_ = -1;
    return false;
  }

  // Clear O_NDELAY to make it blocking (we manage timeouts via poll)
  int flags = fcntl(serial_fd_, F_GETFL, 0);
  fcntl(serial_fd_, F_SETFL, flags & ~O_NDELAY);

  struct termios options;
  if (tcgetattr(serial_fd_, &options) != 0) {
    close(serial_fd_);
    serial_fd_ = -1;
    return false;
  }

  speed_t speed = B9600;
  if (baudrate_ == 115200) {
    speed = B115200;
  }

  cfsetispeed(&options, speed);
  cfsetospeed(&options, speed);

  options.c_cflag &= ~PARENB;
  options.c_cflag &= ~CSTOPB;
  options.c_cflag &= ~CSIZE;
  options.c_cflag |= CS8;
  options.c_cflag |= (CLOCAL | CREAD);

  options.c_lflag &= ~(ICANON | ECHO | ECHOE | ISIG);
  options.c_oflag &= ~OPOST;
  options.c_iflag &=
    ~(IXON | IXOFF | IXANY | IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL);

  options.c_cc[VMIN] = 0;
  options.c_cc[VTIME] = 1;

  if (tcsetattr(serial_fd_, TCSANOW, &options) != 0) {
    close(serial_fd_);
    serial_fd_ = -1;
    return false;
  }

  return true;
}

void SonarDriver::close_port()
{
  if (serial_fd_ >= 0) {
    close(serial_fd_);
    serial_fd_ = -1;
    log_info("Closed serial port: " + port_);
  }
}

void SonarDriver::activate_dome()
{
  dome_active_ = false;
  if (serial_fd_ < 0) {
    return;
  }
  uint8_t wake_up_cmd[] = {0x02, 0x41, 0x31, 0x03};
  ssize_t written = write(serial_fd_, wake_up_cmd, sizeof(wake_up_cmd));
  if (written != static_cast<ssize_t>(sizeof(wake_up_cmd))) {
    log_error(
      "Failed to write sonar dome wake-up command to " + port_ + " (wrote " +
      std::to_string(written) + "/" + std::to_string(sizeof(wake_up_cmd)) +
      " bytes) — dome activation NOT confirmed");
    return;
  }
  tcdrain(serial_fd_);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  // The protocol has no acknowledgement for this command: this only means
  // the bytes reached the port, not that the dome actually woke up.
  dome_active_ = true;
  log_info("Sonar dome wake-up command sent on " + port_);
}

int SonarDriver::query_sonar(int sonar_id)
{
  if (serial_fd_ < 0) {
    return kBusUnavailable;
  }

  uint8_t cmd_byte = 0x30 + sonar_id;
  uint8_t checksum = 0xCC ^ cmd_byte;
  uint8_t request_packet[] = {0xCC, cmd_byte, checksum};

  // Clear old read buffer
  tcflush(serial_fd_, TCIFLUSH);

  ssize_t written = write(serial_fd_, request_packet, sizeof(request_packet));
  if (written != static_cast<ssize_t>(sizeof(request_packet))) {
    return kBusUnavailable;
  }
  tcdrain(serial_fd_);

  uint8_t response[5];
  if (read_bytes(response, 5, kResponseTimeoutMs)) {
    if (response[0] == 0xCC) {
      uint8_t calc_checksum = response[0] ^ response[1] ^ response[2] ^ response[3];
      if (calc_checksum == response[4]) {
        int distance = response[2] | (response[3] << 8);
        return distance;
      }
    }
    tcflush(serial_fd_, TCIFLUSH);
  }

  return -1;
}

bool SonarDriver::read_bytes(uint8_t * buf, size_t len, int timeout_ms)
{
  // One deadline for the whole frame, not one timeout per poll() call: a reply
  // arriving byte-by-byte would otherwise cost up to len * timeout_ms on a
  // single sensor and blow up the round-robin cycle time that bounds how old
  // the safety consumers' data can be.
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);

  size_t bytes_read = 0;
  while (bytes_read < len) {
    const auto remaining_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                deadline - std::chrono::steady_clock::now())
                                .count();
    if (remaining_ms <= 0) {
      return false;
    }

    struct pollfd pfd;
    pfd.fd = serial_fd_;
    pfd.events = POLLIN;
    int ret = poll(&pfd, 1, static_cast<int>(remaining_ms));
    if (ret <= 0) {
      return false;
    }
    ssize_t r = read(serial_fd_, buf + bytes_read, len - bytes_read);
    if (r <= 0) {
      return false;
    }
    bytes_read += r;
  }
  return true;
}

void SonarDriver::run_loop()
{
  while (running_) {
    for (int id : sonar_ids_) {
      if (!running_) {
        break;
      }
      int dist = query_sonar(id);
      {
        std::lock_guard<std::mutex> lock(mutex_);
        SonarReading & reading = readings_[id];
        reading.range = dist;
        reading.seq++;
        reading.stamp = std::chrono::steady_clock::now();
        // Only a failed transaction counts as an error: 0xFFFF is a valid
        // reply meaning "nothing in range", so it clears the counter just
        // like a distance would.
        if (dist < 0) {
          reading.consecutive_errors++;
        } else {
          reading.consecutive_errors = 0;
        }
      }
      // Settle after every poll that actually pinged, including a timed-out
      // one. A timeout means the answer was late, not that it will never
      // come: it lands in the next sensor's read window, and query_sonar
      // validates only the 0xCC header and the checksum, so a complete frame
      // from the previous sensor passes both and is credited to the wrong
      // sensor. Skipping the settle here was tried and is what turns one slow
      // sensor into a cascade of wrong or missing readings.
      // Only a request that never went out skips the wait — there is no echo
      // in flight to avoid, and the short backoff just keeps an unplugged
      // adapter from spinning the loop.
      if (dist == kBusUnavailable) {
        std::this_thread::sleep_for(std::chrono::milliseconds(kErrorBackoffMs));
      } else {
        std::this_thread::sleep_for(std::chrono::milliseconds(kSettleMs));
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
}

}  // namespace guide_robot_sonar
