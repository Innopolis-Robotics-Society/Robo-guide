#include <cstdio>
#include <string>
#include <utility>

#include "guide_robot_supervisor/watchdogs.hpp"

namespace guide_robot_supervisor {

namespace {

/// Кадр из сообщения совпадает с ожидаемым; ведущий '/' в сообщении игнорируется.
bool sameFrame(const std::string & from_msg, const std::string & wanted)
{
  if (!from_msg.empty() && from_msg.front() == '/') {
    return from_msg.compare(1, std::string::npos, wanted) == 0;
  }
  return from_msg == wanted;
}

}  // namespace

// ------------------------------------------------------------------ tracker --

TfEdgeTracker::TfEdgeTracker(rclcpp::Node & node, const rclcpp::CallbackGroup::SharedPtr & group)
: clock_(node.get_clock())
{
  // та же QoS, что у tf2_ros::TransformListener для /tf
  const auto qos = rclcpp::QoS(rclcpp::KeepLast(100)).reliable().durability_volatile();
  rclcpp::SubscriptionOptions options;
  options.callback_group = group;
  sub_ = node.create_subscription<tf2_msgs::msg::TFMessage>(
    "/tf", qos, [this](const tf2_msgs::msg::TFMessage & msg) { onTf(msg); }, options);
}

size_t TfEdgeTracker::watch(const std::string & parent, const std::string & child)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  edges_.push_back(Edge{parent, child, std::nullopt});
  return edges_.size() - 1;
}

std::optional<double> TfEdgeTracker::lastSeen(size_t edge) const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return edges_.at(edge).last_seen;
}

void TfEdgeTracker::forget(size_t edge)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  edges_.at(edge).last_seen.reset();
}

void TfEdgeTracker::onTf(const tf2_msgs::msg::TFMessage & msg)
{
  const double stamp = clock_->now().seconds();
  const std::lock_guard<std::mutex> lock(mutex_);
  for (const auto & tf : msg.transforms) {
    for (auto & edge : edges_) {
      if (sameFrame(tf.header.frame_id, edge.parent) && sameFrame(tf.child_frame_id, edge.child)) {
        edge.last_seen = stamp;
      }
    }
  }
}

// ----------------------------------------------------------------- watchdog --

TfEdgeWatchdog::TfEdgeWatchdog(std::string name, WatchdogContext & ctx, TfEdgeParams params)
: Watchdog(std::move(name), ctx),
  params_(std::move(params)),
  tracker_(ctx.tfTracker()),
  edge_(tracker_->watch(params_.parent, params_.child)),
  grace_(now(), params_.grace)
{
}

Status TfEdgeWatchdog::check()
{
  const std::string chain = params_.parent + " -> " + params_.child;
  const double t = now();
  const auto last_seen = tracker_->lastSeen(edge_);
  if (!last_seen) {
    const Level level = grace_.open(t) ? Level::WARN : Level::ERROR;
    return Status{level, chain + " unavailable: never seen on /tf", {}};
  }

  const double age = t - *last_seen;
  char buf[48];
  std::snprintf(buf, sizeof(buf), "%.2f s", age);
  Status status{Level::OK, chain, {{"age", buf}}};
  if (age > params_.max_age) {
    std::snprintf(buf, sizeof(buf), "%.1f s", age);
    status.level = Level::STALE;
    status.message = chain + " stale (" + buf + ")";
  }
  return status;
}

void TfEdgeWatchdog::reset()
{
  grace_.restart(now());
  tracker_->forget(edge_);
}

}  // namespace guide_robot_supervisor
