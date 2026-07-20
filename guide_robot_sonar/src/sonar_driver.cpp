#include "guide_robot_sonar/sonar_driver.hpp"

#include <fcntl.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <chrono>
#include <iostream>

namespace guide_robot_sonar {

SonarDriver::SonarDriver(const std::string & port, int baudrate) : port_(port), baudrate_(baudrate)
{
  for (int id : sonar_ids_) {
    latest_ranges_[id] = -1;
  }
}

SonarDriver::~SonarDriver()
{
  stop();
}

void SonarDriver::start()
{
  if (running_) {
    return;
  }

  if (!open_port()) {
    std::cerr << "❌ Failed to open serial port: " << port_ << std::endl;
    return;
  }

  std::cout << "✅ Successfully opened serial port: " << port_ << std::endl;

  activate_dome();

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

std::map<int, int> SonarDriver::get_ranges()
{
  std::lock_guard<std::mutex> lock(mutex_);
  return latest_ranges_;
}

bool SonarDriver::open_port()
{
  serial_fd_ = open(port_.c_str(), O_RDWR | O_NOCTTY | O_NDELAY);
  if (serial_fd_ < 0) {
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
    std::cout << "🔒 Closed serial port: " << port_ << std::endl;
  }
}

void SonarDriver::activate_dome()
{
  if (serial_fd_ < 0) {
    return;
  }
  std::cout << "⏰ Activating sonar dome..." << std::endl;
  uint8_t wake_up_cmd[] = {0x02, 0x41, 0x31, 0x03};
  write(serial_fd_, wake_up_cmd, sizeof(wake_up_cmd));
  tcdrain(serial_fd_);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  std::cout << "🚀 Sonar dome is active." << std::endl;
}

int SonarDriver::query_sonar(int sonar_id)
{
  if (serial_fd_ < 0) {
    return -1;
  }

  uint8_t cmd_byte = 0x30 + sonar_id;
  uint8_t checksum = 0xCC ^ cmd_byte;
  uint8_t request_packet[] = {0xCC, cmd_byte, checksum};

  // Clear old read buffer
  tcflush(serial_fd_, TCIFLUSH);

  write(serial_fd_, request_packet, sizeof(request_packet));
  tcdrain(serial_fd_);

  uint8_t response[5];
  if (read_bytes(response, 5, 40)) {
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
  size_t bytes_read = 0;
  while (bytes_read < len) {
    struct pollfd pfd;
    pfd.fd = serial_fd_;
    pfd.events = POLLIN;
    int ret = poll(&pfd, 1, timeout_ms);
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
        latest_ranges_[id] = dist;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
}

}  // namespace guide_robot_sonar
