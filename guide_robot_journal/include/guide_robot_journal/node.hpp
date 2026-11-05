#pragma once
#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <rclcpp/rclcpp.hpp>

#include "guide_robot_journal/core.hpp"

namespace guide_robot_journal {
class JournalNode : public rclcpp::Node
{
public:
  explicit JournalNode(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());
  ~JournalNode() override;

private:
  struct State {
    Record record;
    size_t source{};
    bool assigned{false}, seen{false};
    char component[96]{}, hardware[64]{};
    uint64_t signature{}, previous_count{};
    int64_t previous_snapshot{};
    explicit State(size_t capacity) : record(capacity) {}
  };
  Config config_;
  Counters counters_;
  std::unique_ptr<Writer> writer_;
  std::vector<State> states_;
  std::vector<rclcpp::SubscriptionBase::SharedPtr> subscriptions_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_;
  rclcpp::TimerBase::SharedPtr snapshot_timer_, freshness_timer_, diagnostics_timer_;
  uint64_t sequence_{0}, snapshot_{0};
  std::string instance_id_;
  Record self_record_{sizeof(Compact)};
  void subscribe(size_t index);
  void compact(size_t index, const Compact & data, int64_t stamp = -1);
  void received(size_t index, int64_t stamp);
  void emit(size_t index, const char * event = nullptr);
  void snapshots();
  void freshness();
  void diagnostics();
  void diagnostic_message(size_t source, const diagnostic_msgs::msg::DiagnosticArray & message);
};
}  // namespace guide_robot_journal
