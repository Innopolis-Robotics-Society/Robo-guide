#ifndef GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_
#define GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace guide_robot_sonar {

// One sensor's latest poll result. Error accounting lives here, in the driver,
// because it must be counted per bus transaction: the ROS node republishes the
// cached value several times between two polls of the same sensor, so counting
// failures on the node's publish tick over-counts a single bus fault by the
// ratio of publish rate to poll rate (~5x at 20 Hz).
struct SonarReading {
  // Raw echo time-of-flight counts; 65535 (0xFFFF) = no echo / nothing in
  // range; -1 = the request went out but the answer was missing or malformed;
  // -2 = the request could not be sent (port gone). Also -1 before the first
  // poll (seq == 0).
  int range{-1};
  // Consecutive failed polls of this sensor. Reset by any successful
  // transaction, including a valid "no echo" reply. Zero until the first poll.
  int consecutive_errors{0};
  // Completed polls of this sensor since start(). Lets a consumer tell a fresh
  // reading from a cached one it has already seen.
  uint64_t seq{0};
  // Seconds since this reading was taken, evaluated inside get_readings().
  // Before the first poll it counts from start() — or from construction if
  // start() never got as far as launching the poll thread — so a driver that
  // is not polling at all (port failed to open, thread wedged) reads as stale
  // instead of ageless. Callers must treat stale the same as a bus fault:
  // an old distance under a fresh header stamp is indistinguishable from a
  // current one downstream.
  double age_s{0.0};
  // Not exposed to Python: source for age_s above.
  std::chrono::steady_clock::time_point stamp{};
};

class SonarDriver
{
public:
  using LogFn = std::function<void(const std::string &)>;

  SonarDriver(const std::string & port, int baudrate);
  ~SonarDriver();

  // Route driver log messages through the caller's logger (e.g. rclpy's
  // get_logger()) instead of raw stdout/stderr. If unset, falls back to
  // std::cerr so the driver stays usable standalone (e.g. in tests).
  void set_info_logger(LogFn fn);
  void set_error_logger(LogFn fn);

  void start();
  void stop();

  // Returns the latest reading per sensor: {sensor_id -> SonarReading}.
  // Ranges are not millimeters — the byte pair is a raw counter proportional
  // to echo travel time; callers convert to meters via a calibrated
  // scale/offset. See SonarReading for the per-field contract.
  std::map<int, SonarReading> get_readings();

  // Best-effort: true only if the wake-up command was written to the port
  // in full. The dome protocol has no acknowledgement, so this cannot
  // confirm the dome actually woke up — only that the write() didn't fail.
  bool is_dome_active() const;

private:
  void run_loop();
  bool open_port();
  void close_port();
  void activate_dome();
  int query_sonar(int sonar_id);
  bool read_bytes(uint8_t * buf, size_t len, int timeout_ms);
  void log_info(const std::string & msg);
  void log_error(const std::string & msg);

  std::string port_;
  int baudrate_;
  int serial_fd_{-1};
  std::atomic<bool> running_{false};
  std::atomic<bool> dome_active_{false};
  std::thread thread_;
  std::mutex mutex_;
  std::map<int, SonarReading> readings_;
  std::vector<int> sonar_ids_{0, 3, 1, 4, 2, 5, 6};

  std::mutex log_mutex_;
  LogFn info_logger_;
  LogFn error_logger_;
};

}  // namespace guide_robot_sonar

#endif  // GUIDE_ROBOT_SONAR__SONAR_DRIVER_HPP_
