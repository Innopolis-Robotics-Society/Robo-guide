#include "guide_robot_supervisor/watchdog.hpp"

#include <utility>
#include <variant>

#include "guide_robot_supervisor/watchdogs.hpp"

namespace guide_robot_supervisor {

WatchdogContext::WatchdogContext(rclcpp::Node & node, rclcpp::CallbackGroup::SharedPtr group)
: node_(node), group_(std::move(group))
{
}

std::shared_ptr<TfEdgeTracker> WatchdogContext::tfTracker()
{
  if (!tf_tracker_) tf_tracker_ = std::make_shared<TfEdgeTracker>(node_, group_);
  return tf_tracker_;
}

Watchdog::Watchdog(std::string name, WatchdogContext & ctx)
: name_(std::move(name)), ctx_(ctx), logger_(ctx.node().get_logger().get_child(name_))
{
}

double Watchdog::now() const
{
  return ctx_.node().get_clock()->now().seconds();
}

namespace {

template <class... Ts>
struct Overloaded : Ts... {
  using Ts::operator()...;
};
template <class... Ts>
Overloaded(Ts...) -> Overloaded<Ts...>;

}  // namespace

std::unique_ptr<Watchdog> makeWatchdog(const WatchdogSpec & spec, WatchdogContext & ctx)
{
  return std::visit(
    Overloaded{
      [&](const TopicRateParams & p) -> std::unique_ptr<Watchdog> {
        return std::make_unique<TopicRateWatchdog>(spec.name, ctx, p);
      },
      [&](const TfEdgeParams & p) -> std::unique_ptr<Watchdog> {
        return std::make_unique<TfEdgeWatchdog>(spec.name, ctx, p);
      },
      [&](const NodeAliveParams & p) -> std::unique_ptr<Watchdog> {
        return std::make_unique<NodeAliveWatchdog>(spec.name, ctx, p);
      },
      [&](const LifecycleManagerActiveParams & p) -> std::unique_ptr<Watchdog> {
        return std::make_unique<LifecycleManagerActiveWatchdog>(spec.name, ctx, p);
      },
    },
    spec.params);
}

}  // namespace guide_robot_supervisor
