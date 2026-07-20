#ifndef GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_
#define GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_

#include <atomic>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace guide_robot_sonar {

class SonarDriver
{
public:
  SonarDriver(const std::string & port, int baudrate);
  ~SonarDriver();

  void start();
  void stop();

  // Returns latest range measurements: {sensor_id -> distance_mm}
  // If no obstacle/out of range, distance is 65535 (0xFFFF)
  // If timeout/read error, distance is -1
  std::map<int, int> get_ranges();

private:
  void run_loop();
  bool open_port();
  void close_port();
  void activate_dome();
  int query_sonar(int sonar_id);
  bool read_bytes(uint8_t * buf, size_t len, int timeout_ms);

  std::string port_;
  int baudrate_;
  int serial_fd_{-1};
  std::atomic<bool> running_{false};
  std::thread thread_;
  std::mutex mutex_;
  std::map<int, int> latest_ranges_;
  std::vector<int> sonar_ids_{0, 1, 2, 3, 4, 5, 6};
};

}  // namespace guide_robot_sonar

#endif  // GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_
