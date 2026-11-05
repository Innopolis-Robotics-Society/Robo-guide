#include <gtest/gtest.h>
#include <unistd.h>
#include <yaml-cpp/yaml.h>

#include <cmath>
#include <fstream>
#include <map>
#include <rclcpp/serialization.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <std_msgs/msg/string.hpp>

#include "guide_robot_journal/node.hpp"

using namespace guide_robot_journal;
namespace fs = std::filesystem;
namespace {
std::vector<uint8_t> decode(const std::string & s)
{
  const std::string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::vector<uint8_t> out;
  uint32_t value = 0;
  int bits = 0;
  for (char c : s) {
    if (c == '=') break;
    value = (value << 6) | alphabet.find(c);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back((value >> bits) & 255);
    }
  }
  return out;
}
}  // namespace
TEST(JournalRos, FullLidarAndPartialDiagnosticsWithPausedRosClock)
{
  char tmp[] = "/tmp/guide-journal-ros-XXXXXX";
  fs::path path = ::mkdtemp(tmp);
  auto config = path / "config.yaml";
  std::ofstream(config)
    << "storage:\n  directory: " << (path / "logs").string()
    << "\n  reserve_bytes: 0\n"
       "timing:\n  snapshot_s: 0.05\n  freshness_s: 0.02\n  flush_s: 0.02\n"
       "sources:\n- {id: lidar, topic: /journal_test_scan, adapter: serialized, type: "
       "sensor_msgs/msg/LaserScan, stale_s: 0.1}\n"
       "- {id: diagnostics, topic: /journal_test_diag, adapter: diagnostics, stale_s: 0.1}\n";
  rclcpp::init(0, nullptr);
  auto node = std::make_shared<JournalNode>(rclcpp::NodeOptions().parameter_overrides(
    {rclcpp::Parameter("config_file", config.string()), rclcpp::Parameter("use_sim_time", true)}));
  auto source = std::make_shared<rclcpp::Node>("journal_test_source");
  auto scan_pub = source->create_publisher<sensor_msgs::msg::LaserScan>(
    "/journal_test_scan", rclcpp::SensorDataQoS());
  auto diag_pub =
    source->create_publisher<diagnostic_msgs::msg::DiagnosticArray>("/journal_test_diag", 10);
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(node);
  executor.add_node(source);
  auto spin = [&](double seconds) {
    auto until = steady_ns() + int64_t(seconds * 1e9);
    while (steady_ns() < until) {
      executor.spin_some();
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  };
  spin(.2);
  sensor_msgs::msg::LaserScan scan;
  scan.header.frame_id = "laser";
  scan.angle_min = -1;
  scan.angle_max = 1;
  scan.ranges.resize(2048);
  scan.intensities.resize(2048);
  for (size_t i = 0; i < scan.ranges.size(); ++i) {
    scan.ranges[i] = float(i) / 10;
    scan.intensities[i] = float(i);
  }
  scan.ranges[7] = std::numeric_limits<float>::infinity();
  scan.ranges[11] = std::nanf("");
  scan_pub->publish(scan);
  diagnostic_msgs::msg::DiagnosticArray diag;
  diagnostic_msgs::msg::DiagnosticStatus first;
  first.name = "left";
  first.hardware_id = "drive";
  first.message = "OK";
  diag.status = {first};
  diag_pub->publish(diag);
  spin(.08);
  first.name = "right";
  first.level = 2;
  first.message = "fault";
  diag.status = {first};
  diag_pub->publish(diag);
  spin(.08);
  // Oversized scans are rejected WHOLE, not truncated. The valid scan already has a snapshot.
  scan.ranges.resize(20000);
  scan_pub->publish(scan);
  spin(.2);
  executor.remove_node(node);
  node.reset();
  executor.remove_node(source);
  source.reset();
  rclcpp::shutdown();
  bool lidar = false, left = false, right = false, stale = false, rejected = false;
  std::map<uint64_t, int> snapshot_components;
  for (auto & entry : fs::directory_iterator(path / "logs")) {
    if (entry.path().extension() != ".jsonl") continue;
    std::ifstream in(entry.path());
    std::string line;
    while (std::getline(in, line)) {
      auto row = YAML::Load(line);
      if (row["component"]) {
        if (row["type"].as<std::string>() == "snapshot") {
          auto component = row["component"].as<std::string>();
          snapshot_components[row["snapshot_id"].as<uint64_t>()] |= component == "left"    ? 1
                                                                    : component == "right" ? 2
                                                                                           : 0;
        }
        left |= row["component"].as<std::string>() == "left";
        right |= row["component"].as<std::string>() == "right";
      }
      if (row["event"].as<std::string>() == "source_stale") stale = true;
      if (row["source"].as<std::string>() != "lidar") continue;
      if (row["received"].as<int>() >= 2 && !row["payload_available"].as<bool>()) rejected = true;
      if (!row["encoding"]) continue;
      auto raw = decode(row["data"].as<std::string>());
      rclcpp::SerializedMessage serialized(raw.size());
      auto & buffer = serialized.get_rcl_serialized_message();
      std::memcpy(buffer.buffer, raw.data(), raw.size());
      buffer.buffer_length = raw.size();
      sensor_msgs::msg::LaserScan decoded;
      rclcpp::Serialization<sensor_msgs::msg::LaserScan> codec;
      codec.deserialize_message(&serialized, &decoded);
      EXPECT_EQ(decoded.ranges.size(), 2048);
      EXPECT_EQ(decoded.intensities.size(), 2048);
      EXPECT_EQ(decoded.intensities.back(), 2047);
      EXPECT_TRUE(std::isinf(decoded.ranges[7]));
      EXPECT_TRUE(std::isnan(decoded.ranges[11]));
      EXPECT_EQ(decoded.header.frame_id, "laser");
      EXPECT_EQ(row["ros_ns"].as<int64_t>(), 0);
      lidar = true;
    }
  }
  EXPECT_TRUE(lidar);
  EXPECT_TRUE(left);
  EXPECT_TRUE(right);
  EXPECT_TRUE(stale);
  EXPECT_TRUE(rejected);
  bool complete_snapshot = false;
  for (const auto & entry : snapshot_components) complete_snapshot |= entry.second == 3;
  EXPECT_TRUE(complete_snapshot);
  fs::remove_all(path);
}
