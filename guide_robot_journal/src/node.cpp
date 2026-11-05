#include "guide_robot_journal/node.hpp"

#include <unistd.h>

#include <algorithm>
#include <geometry_msgs/msg/twist.hpp>
#include <guide_robot_msgs/msg/mission_state.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <sensor_msgs/msg/battery_state.hpp>
#include <std_msgs/msg/bool.hpp>
#include <std_msgs/msg/string.hpp>
#include <stdexcept>

namespace guide_robot_journal {
namespace {
struct CallbackTimer {
  Counters & stats;
  int64_t started{steady_ns()};
  ~CallbackTimer()
  {
    const auto elapsed = uint64_t(steady_ns() - started);
    if (elapsed > stats.max_callback_ns.load()) stats.max_callback_ns = elapsed;
  }
};
int64_t stamp_ns(const builtin_interfaces::msg::Time & t)
{
  return int64_t(t.sec) * 1000000000 + t.nanosec;
}
void numeric(Compact & data, const char * key, double value)
{
  if (data.count == kFields) {
    data.truncated = true;
    return;
  }
  auto & field = data.fields[data.count++];
  text(field.key, sizeof(field.key), key);
  field.numeric = true;
  field.number = value;
}
uint64_t hash_text(uint64_t hash, const char * value)
{
  for (const auto * p = value; *p; ++p) {
    hash ^= uint8_t(*p);
    hash *= 1099511628211ULL;
  }
  return (hash ^ 255) * 1099511628211ULL;
}
uint64_t signature(const Compact & data, const Source & source)
{
  uint64_t hash = hash_text(1469598103934665603ULL, data.text);
  hash = (hash ^ data.level) * 1099511628211ULL;
  for (const auto & key : source.event_fields)
    for (size_t i = 0; i < data.count; ++i)
      if (key == data.fields[i].key) hash = hash_text(hash, data.fields[i].text);
  if (source.adapter == "mission") {
    for (size_t i = 0; i < data.count; ++i) {
      if (data.fields[i].numeric) {
        uint64_t bits;
        std::memcpy(&bits, &data.fields[i].number, sizeof(bits));
        hash = (hash ^ bits) * 1099511628211ULL;
      } else
        hash = hash_text(hash, data.fields[i].text);
    }
  }
  return hash;
}
rclcpp::QoS qos(const Source & source)
{
  rclcpp::QoS q(source.depth);
  if (source.reliability == "best_effort")
    q.best_effort();
  else
    q.reliable();
  if (source.durability == "transient_local")
    q.transient_local();
  else
    q.durability_volatile();
  return q;
}
}  // namespace
JournalNode::JournalNode(const rclcpp::NodeOptions & options) : Node("robot_journal", options)
{
  const auto file = declare_parameter<std::string>("config_file", "");
  if (file.empty()) throw std::runtime_error("config_file must be supplied");
  config_ = load_config(file);
  instance_id_ = std::to_string(utc_ns()) + "-" + std::to_string(::getpid());
  diagnostics_ =
    create_publisher<diagnostic_msgs::msg::DiagnosticArray>("~/diagnostics", rclcpp::QoS(1));
  states_.reserve(config_.sources.size() + config_.max_components);
  for (size_t i = 0; i < config_.sources.size(); ++i) {
    const auto & source = config_.sources[i];
    states_.emplace_back(source.adapter == "serialized" ? source.max_bytes : sizeof(Compact));
    auto & state = states_.back();
    state.source = i;
    state.assigned = true;
    state.previous_snapshot = steady_ns();
    text(state.record.header.source, sizeof(state.record.header.source), source.id);
    text(state.record.header.topic, sizeof(state.record.header.topic), source.topic);
    text(state.record.header.ros_type, sizeof(state.record.header.ros_type), source.type);
  }
  for (size_t i = 0; i < config_.max_components; ++i) states_.emplace_back(sizeof(Compact));
  writer_ = std::make_unique<Writer>(config_, counters_);
  for (size_t i = 0; i < config_.sources.size(); ++i) subscribe(i);
  snapshot_timer_ =
    create_wall_timer(std::chrono::duration<double>(config_.snapshot_s), [this] { snapshots(); });
  freshness_timer_ =
    create_wall_timer(std::chrono::duration<double>(config_.freshness_s), [this] { freshness(); });
  diagnostics_timer_ = create_wall_timer(std::chrono::seconds(5), [this] { diagnostics(); });
  text(self_record_.header.event, sizeof(self_record_.header.event), "logger_started");
  diagnostics();
  self_record_.header.event[0] = 0;
  RCLCPP_INFO(
    get_logger(),
    "journal: %zu sources, owned memory estimate %zu bytes; disk work on separate thread",
    config_.sources.size(), config_.owned_memory_bound());
}
JournalNode::~JournalNode()
{
  if (writer_) writer_->stop();
}
void JournalNode::received(size_t index, int64_t stamp)
{
  auto & s = states_[index];
  auto & h = s.record.header;
  const bool stale = h.freshness == 2;
  h.last_received_ns = steady_ns();
  h.source_ns = stamp;
  h.source_stamp_available = stamp >= 0;
  ++h.received;
  h.freshness = config_.sources[s.source].stale_s == 0 ? 3 : 1;
  if (stale) emit(index, "source_restored");
}
void JournalNode::emit(size_t index, const char * event)
{
  auto & r = states_[index].record;
  text(r.header.instance_id, sizeof(r.header.instance_id), instance_id_);
  r.header.sequence = ++sequence_;
  r.header.utc_ns = utc_ns();
  r.header.monotonic_ns = steady_ns();
  r.header.ros_ns = now().nanoseconds();
  r.header.snapshot = event ? 0 : snapshot_;
  text(r.header.event, sizeof(r.header.event), event ? event : "");
  // Events for large serialized sources contain metadata only, never a truncated scan.
  if (event && r.header.encoding == Encoding::Cdr) {
    const auto used = r.used;
    const auto encoding = r.header.encoding;
    r.used = 0;
    r.header.encoding = Encoding::Arrival;
    writer_->push(r, true);
    r.used = used;
    r.header.encoding = encoding;
  } else
    writer_->push(r, event != nullptr);
  r.header.event[0] = 0;
}
void JournalNode::compact(size_t index, const Compact & data, int64_t stamp)
{
  auto & s = states_[index];
  const auto & source = config_.sources[s.source];
  const auto sig = signature(data, source);
  const bool changed = !s.seen || s.signature != sig;
  s.record.header.encoding = Encoding::Compact;
  s.record.header.payload_available = true;
  s.record.header.truncated = data.truncated;
  s.record.used = sizeof(data);
  std::memcpy(s.record.data.data(), &data, sizeof(data));
  received(index, stamp);
  if (data.truncated) ++counters_.input_limits;
  if (changed && source.events)
    emit(index, s.seen ? "state_changed" : "first_state");
  else if (!changed)
    ++s.record.header.unchanged;
  s.seen = true;
  s.signature = sig;
}
void JournalNode::subscribe(size_t i)
{
  const auto & s = config_.sources[i];
  if (s.topic == diagnostics_->get_topic_name())
    throw std::runtime_error("self diagnostics source is forbidden");
  auto type = [&](const char * name) { text(states_[i].record.header.ros_type, 96, name); };
  if (s.adapter == "string") {
    type("std_msgs/msg/String");
    subscriptions_.push_back(create_subscription<std_msgs::msg::String>(
      s.topic, qos(s), [this, i](std_msgs::msg::String::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        data.truncated = text(data.text, sizeof(data.text), msg->data);
        compact(i, data);
      }));
  } else if (s.adapter == "bool") {
    type("std_msgs/msg/Bool");
    subscriptions_.push_back(create_subscription<std_msgs::msg::Bool>(
      s.topic, qos(s), [this, i](std_msgs::msg::Bool::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        text(data.text, sizeof(data.text), msg->data ? "true" : "false");
        numeric(data, "value", msg->data ? 1 : 0);
        compact(i, data);
      }));
  } else if (s.adapter == "twist") {
    type("geometry_msgs/msg/Twist");
    subscriptions_.push_back(create_subscription<geometry_msgs::msg::Twist>(
      s.topic, qos(s), [this, i](geometry_msgs::msg::Twist::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        numeric(data, "linear_x", msg->linear.x);
        numeric(data, "linear_y", msg->linear.y);
        numeric(data, "linear_z", msg->linear.z);
        numeric(data, "angular_x", msg->angular.x);
        numeric(data, "angular_y", msg->angular.y);
        numeric(data, "angular_z", msg->angular.z);
        compact(i, data);
      }));
  } else if (s.adapter == "battery") {
    type("sensor_msgs/msg/BatteryState");
    subscriptions_.push_back(create_subscription<sensor_msgs::msg::BatteryState>(
      s.topic, qos(s), [this, i](sensor_msgs::msg::BatteryState::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        data.level = msg->power_supply_health;
        text(data.text, sizeof(data.text), std::to_string(msg->power_supply_status));
        numeric(data, "voltage", msg->voltage);
        numeric(data, "current", msg->current);
        numeric(data, "percentage", msg->percentage);
        numeric(data, "charge", msg->charge);
        numeric(data, "capacity", msg->capacity);
        numeric(data, "temperature", msg->temperature);
        numeric(data, "present", msg->present ? 1 : 0);
        compact(i, data, stamp_ns(msg->header.stamp));
      }));
  } else if (s.adapter == "odometry") {
    type("nav_msgs/msg/Odometry");
    subscriptions_.push_back(create_subscription<nav_msgs::msg::Odometry>(
      s.topic, qos(s), [this, i](nav_msgs::msg::Odometry::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        const auto & p = msg->pose.pose;
        const auto & v = msg->twist.twist;
        numeric(data, "x", p.position.x);
        numeric(data, "y", p.position.y);
        numeric(data, "z", p.position.z);
        numeric(data, "qx", p.orientation.x);
        numeric(data, "qy", p.orientation.y);
        numeric(data, "qz", p.orientation.z);
        numeric(data, "qw", p.orientation.w);
        numeric(data, "linear_x", v.linear.x);
        numeric(data, "linear_y", v.linear.y);
        numeric(data, "linear_z", v.linear.z);
        numeric(data, "angular_x", v.angular.x);
        numeric(data, "angular_y", v.angular.y);
        numeric(data, "angular_z", v.angular.z);
        compact(i, data, stamp_ns(msg->header.stamp));
      }));
  } else if (s.adapter == "mission") {
    type("guide_robot_msgs/msg/MissionState");
    subscriptions_.push_back(create_subscription<guide_robot_msgs::msg::MissionState>(
      s.topic, qos(s), [this, i](guide_robot_msgs::msg::MissionState::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        Compact data;
        data.level = msg->state;
        data.truncated = text(data.text, sizeof(data.text), msg->detail);
        numeric(data, "state", msg->state);
        numeric(data, "base_state", msg->base_state);
        numeric(data, "interrupt", msg->interrupt);
        numeric(data, "pause_reason", msg->pause_reason);
        numeric(data, "stop_index", msg->stop_index);
        numeric(data, "stop_total", msg->stop_total);
        for (const auto & pair :
             {std::make_pair("tour_id", &msg->tour_id), std::make_pair("stop_id", &msg->stop_id)}) {
          auto & f = data.fields[data.count++];
          text(f.key, sizeof(f.key), pair.first);
          data.truncated |= text(f.text, sizeof(f.text), *pair.second);
        }
        compact(i, data, stamp_ns(msg->header.stamp));
      }));
  } else if (s.adapter == "diagnostics") {
    type("diagnostic_msgs/msg/DiagnosticArray");
    subscriptions_.push_back(create_subscription<diagnostic_msgs::msg::DiagnosticArray>(
      s.topic, qos(s), [this, i](diagnostic_msgs::msg::DiagnosticArray::ConstSharedPtr msg) {
        CallbackTimer measured{counters_};
        received(i, stamp_ns(msg->header.stamp));
        diagnostic_message(i, *msg);
      }));
  } else {
    subscriptions_.push_back(create_generic_subscription(
      s.topic, s.type, qos(s), [this, i](std::shared_ptr<rclcpp::SerializedMessage> msg) {
        CallbackTimer measured{counters_};
        auto & state = states_[i];
        auto & record = state.record;
        const auto & source = config_.sources[i];
        bool rejected = false, restored = false;
        if (source.adapter == "serialized") {
          const bool previous = record.header.payload_available;
          const auto & raw = msg->get_rcl_serialized_message();
          record.header.encoding = Encoding::Cdr;
          record.header.payload_available = raw.buffer_length <= record.data.size();
          record.header.truncated = false;
          record.used = record.header.payload_available ? raw.buffer_length : 0;
          rejected = !record.header.payload_available && (!state.seen || previous);
          restored = record.header.payload_available && state.seen && !previous;
          if (record.used)
            std::memcpy(record.data.data(), raw.buffer, record.used);
          else if (!record.header.payload_available)
            ++counters_.input_limits;
        }
        received(i, -1);
        if (rejected) emit(i, "payload_rejected");
        if (restored) emit(i, "payload_restored");
        state.seen = true;
        if (source.stream) {
          record.header.snapshot = 0;  // stream is not a synchronized snapshot
          const auto saved = snapshot_;
          snapshot_ = 0;
          emit(i);
          snapshot_ = saved;
        }
      }));
  }
}
void JournalNode::diagnostic_message(
  size_t source, const diagnostic_msgs::msg::DiagnosticArray & message)
{
  const auto count = std::min(config_.max_statuses_per_message, message.status.size());
  counters_.input_limits += message.status.size() - count;
  for (size_t j = 0; j < count; ++j) {
    const auto & status = message.status[j];
    if (status.name == "robot_journal" || status.name.rfind("robot_journal/", 0) == 0) continue;
    // Never merge two distinct keys due to truncation.
    if (
      status.name.size() >= 96 || status.hardware_id.size() >= 64 ||
      status.name.find('\0') != std::string::npos ||
      status.hardware_id.find('\0') != std::string::npos) {
      ++counters_.input_limits;
      continue;
    }
    size_t selected = states_.size(), free = states_.size();
    for (size_t k = config_.sources.size(); k < states_.size(); ++k) {
      auto & state = states_[k];
      if (!state.assigned) {
        if (free == states_.size()) free = k;
        continue;
      }
      if (
        state.source == source && status.name == state.component &&
        status.hardware_id == state.hardware) {
        selected = k;
        break;
      }
    }
    if (selected == states_.size()) {
      if (free == states_.size()) {
        ++counters_.input_limits;
        continue;
      }
      selected = free;
      auto & state = states_[selected];
      state.assigned = true;
      state.source = source;
      state.record.header = states_[source].record.header;
      state.record.header.received = 0;
      state.previous_snapshot = steady_ns();
      text(state.component, sizeof(state.component), status.name);
      text(state.hardware, sizeof(state.hardware), status.hardware_id);
    }
    Compact data;
    data.level = status.level;
    text(data.component, sizeof(data.component), status.name);
    text(data.hardware, sizeof(data.hardware), status.hardware_id);
    data.truncated = text(data.text, sizeof(data.text), status.message);
    data.truncated |= status.values.size() > kFields;
    for (size_t k = 0; k < std::min(kFields, status.values.size()); ++k) {
      const auto & value = status.values[k];
      bool duplicate = false;
      for (size_t n = 0; n < data.count; ++n) duplicate |= value.key == data.fields[n].key;
      if (
        value.key.empty() || value.key.find('\0') != std::string::npos ||
        value.key.size() >= sizeof(Field::key) || duplicate) {
        data.truncated = true;
        continue;
      }
      auto & field = data.fields[data.count++];
      text(field.key, sizeof(field.key), value.key);
      data.truncated |= text(field.text, sizeof(field.text), value.value);
    }
    compact(selected, data, stamp_ns(message.header.stamp));
  }
}
void JournalNode::snapshots()
{
  ++snapshot_;
  const auto now_ns = steady_ns();
  for (size_t i = 0; i < states_.size(); ++i) {
    auto & state = states_[i];
    if (!state.assigned) continue;
    if (state.previous_snapshot)
      state.record.header.rate_hz = double(state.record.header.received - state.previous_count) *
                                    1e9 / (now_ns - state.previous_snapshot);
    state.previous_snapshot = now_ns;
    state.previous_count = state.record.header.received;
    emit(i);
  }
}
void JournalNode::freshness()
{
  const auto now_ns = steady_ns();
  for (size_t i = 0; i < states_.size(); ++i) {
    auto & state = states_[i];
    if (!state.assigned || !state.record.header.received) continue;
    const auto timeout = config_.sources[state.source].stale_s;
    if (
      timeout && state.record.header.freshness != 2 &&
      now_ns - state.record.header.last_received_ns > timeout * 1e9) {
      state.record.header.freshness = 2;
      emit(i, "source_stale");
    }
  }
}
void JournalNode::diagnostics()
{
  diagnostic_msgs::msg::DiagnosticArray msg;
  msg.header.stamp = now();
  diagnostic_msgs::msg::DiagnosticStatus status;
  status.name = "robot_journal";
  status.hardware_id = "host";
  status.level = counters_.disk_ok ? 0 : (counters_.write_errors ? 2 : 1);
  if (
    counters_.disk_ok && (counters_.dropped_events || counters_.dropped_snapshots ||
                          counters_.disk_drops || counters_.input_limits))
    status.level = 1;
  status.message =
    counters_.disk_ok ? "recording" : (counters_.write_errors ? "storage_unavailable" : "starting");
  Compact own;
  own.level = status.level;
  text(own.text, sizeof(own.text), status.message);
  auto add = [&](const char * key, uint64_t value) {
    numeric(own, key, double(value));
    diagnostic_msgs::msg::KeyValue field;
    field.key = key;
    field.value = std::to_string(value);
    status.values.push_back(field);
  };
  add("snapshot_queue", writer_->snapshots.size());
  add("event_queue", writer_->events.size());
  add("snapshot_high_water", writer_->snapshots.high_water());
  add("event_high_water", writer_->events.high_water());
  add("dropped_snapshots", counters_.dropped_snapshots);
  add("dropped_events", counters_.dropped_events);
  add("disk_drops", counters_.disk_drops);
  add("write_errors", counters_.write_errors);
  add("last_errno", counters_.last_errno);
  add("input_limits", counters_.input_limits);
  add("retained_bytes", counters_.retained_bytes);
  add("bytes_written", counters_.bytes_written);
  add("written_records", counters_.written_records);
  add("deleted_files", counters_.deleted_files);
  add("owned_memory_bound", config_.owned_memory_bound());
  add("max_callback_ns", counters_.max_callback_ns);
  msg.status.push_back(status);
  diagnostics_->publish(msg);
  auto & r = self_record_;
  text(r.header.source, sizeof(r.header.source), "__journal");
  text(r.header.instance_id, sizeof(r.header.instance_id), instance_id_);
  r.header.sequence = ++sequence_;
  r.header.snapshot = snapshot_;
  r.header.utc_ns = utc_ns();
  r.header.monotonic_ns = steady_ns();
  r.header.ros_ns = now().nanoseconds();
  r.header.encoding = Encoding::Compact;
  r.header.payload_available = true;
  r.header.freshness = 1;
  r.used = sizeof(own);
  std::memcpy(r.data.data(), &own, sizeof(own));
  writer_->push(r, r.header.event[0] != 0);
  if (!counters_.disk_ok && counters_.write_errors)
    RCLCPP_WARN(
      get_logger(), "journal storage unavailable; losses=%lu errors=%lu",
      counters_.disk_drops.load(), counters_.write_errors.load());
}
}  // namespace guide_robot_journal
