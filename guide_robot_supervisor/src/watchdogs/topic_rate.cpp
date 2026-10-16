#include <cstdio>
#include <string>
#include <utility>
#include <vector>

#include "guide_robot_supervisor/watchdogs.hpp"

namespace guide_robot_supervisor {

namespace {

std::string join(const std::vector<std::string> & items)
{
  std::string out;
  for (const auto & item : items) out += (out.empty() ? "" : ", ") + item;
  return out;
}

std::string hz(double rate)
{
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%.2f Hz", rate);
  return buf;
}

}  // namespace

TopicRateWatchdog::TopicRateWatchdog(
  std::string name, WatchdogContext & ctx, TopicRateParams params)
: Watchdog(std::move(name), ctx),
  params_(std::move(params)),
  qos_(params_.sensor_qos ? rclcpp::QoS(rclcpp::SensorDataQoS()) : rclcpp::SystemDefaultsQoS()),
  grace_(now(), params_.grace)
{
  topics_.reserve(params_.topics.size());
  for (const auto & topic : params_.topics) {
    topics_.push_back(Topic{topic, RateEstimator(static_cast<size_t>(params_.window)), nullptr});
  }
  for (size_t i = 0; i < topics_.size(); ++i) {
    trySubscribe(i);
    if (!topics_[i].sub) {
      RCLCPP_WARN(
        logger(), "type of %s is unknown yet, will retry lazily", topics_[i].name.c_str());
    }
  }
}

void TopicRateWatchdog::trySubscribe(size_t index)
{
  Topic & topic = topics_[index];
  std::string type = params_.msg_type;
  if (type.empty()) {
    const auto publishers = node().get_publishers_info_by_topic(topic.name);
    if (publishers.empty()) return;
    type = publishers.front().topic_type();
  }

  rclcpp::SubscriptionOptions options;
  options.callback_group = group();
  try {
    topic.sub = node().create_generic_subscription(
      topic.name, type, qos_,
      [this, index](std::shared_ptr<rclcpp::SerializedMessage> /*msg*/) {
        const double stamp = now();
        const std::lock_guard<std::mutex> lock(mutex_);
        topics_[index].rate.add(stamp);
      },
      options);
  } catch (const std::exception & exc) {
    // Тип не загрузился (нет typesupport-библиотеки, опечатка в msg_type):
    // пробуем на каждом check(), но шумим один раз.
    if (!topic.error_logged) {
      RCLCPP_ERROR(
        logger(), "cannot subscribe to %s as %s: %s", topic.name.c_str(), type.c_str(), exc.what());
      topic.error_logged = true;
    }
  }
}

Status TopicRateWatchdog::check()
{
  for (size_t i = 0; i < topics_.size(); ++i) {
    if (!topics_[i].sub) trySubscribe(i);
  }

  const double t = now();
  Status status;
  std::vector<std::string> dead;
  std::vector<std::string> slow;
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    for (const auto & topic : topics_) {
      // одна оценка на топик: классификация и values не могут разъехаться
      const double rate = topic.rate.rate(t, params_.min_rate);
      status.values.emplace_back(topic.name, hz(rate));
      if (rate < params_.min_rate) {
        dead.push_back(topic.name);
      } else if (rate < params_.warn_rate) {
        slow.push_back(topic.name);
      }
    }
  }

  if (!dead.empty()) {
    if (grace_.open(t)) {
      status.level = Level::WARN;
      status.message = "waiting for " + join(dead);
    } else {
      status.level = Level::ERROR;
      status.message = "no data: " + join(dead);
    }
  } else if (!slow.empty()) {
    status.level = Level::WARN;
    status.message = "low rate: " + join(slow);
  } else {
    status.level = Level::OK;
    status.message = "all topics nominal";
  }
  return status;
}

void TopicRateWatchdog::reset()
{
  grace_.restart(now());
  const std::lock_guard<std::mutex> lock(mutex_);
  for (auto & topic : topics_) topic.rate.clear();
}

}  // namespace guide_robot_supervisor
