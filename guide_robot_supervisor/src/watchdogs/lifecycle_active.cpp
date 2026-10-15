#include <chrono>
#include <future>
#include <string>
#include <utility>

#include "guide_robot_supervisor/watchdogs.hpp"

namespace guide_robot_supervisor {

LifecycleManagerActiveWatchdog::LifecycleManagerActiveWatchdog(
  std::string name, WatchdogContext & ctx, LifecycleManagerActiveParams params)
: Watchdog(std::move(name), ctx),
  params_(std::move(params)),
  client_(node().create_client<std_srvs::srv::Trigger>(
    params_.manager + "/is_active", rmw_qos_profile_services_default, group())),
  grace_(now(), params_.grace),
  last_{Level::WARN, params_.manager + " no answer yet", {}}
{
}

LifecycleManagerActiveWatchdog::~LifecycleManagerActiveWatchdog()
{
  dropPending();
}

void LifecycleManagerActiveWatchdog::dropPending()
{
  if (pending_) {
    client_->remove_pending_request(*pending_);
    pending_.reset();
  }
}

Status LifecycleManagerActiveWatchdog::check()
{
  const auto & manager = params_.manager;
  if (pending_) {
    if (pending_->future.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
      const auto response = pending_->future.get();
      pending_.reset();
      last_ = response->success ? Status{Level::OK, manager + " active", {}}
                                : Status{Level::ERROR, manager + " inactive", {}};
    } else if (
      std::chrono::steady_clock::now() - sent_at_ >
      std::chrono::duration<double>(params_.timeout)) {
      dropPending();
      last_ = Status{Level::STALE, manager + " did not answer", {}};
    } else {
      return last_;  // ответ ещё в пути
    }
  }

  if (!client_->service_is_ready()) {
    // Сервиса нет вовсе — процесс менеджера мёртв или ещё не поднялся.
    // После grace это ERROR: иначе падение менеджера целиком не ловится.
    const Level level = grace_.open(now()) ? Level::WARN : Level::ERROR;
    last_ = Status{level, manager + "/is_active not available", {}};
    return last_;
  }

  pending_.emplace(
    client_->async_send_request(std::make_shared<std_srvs::srv::Trigger::Request>()));
  sent_at_ = std::chrono::steady_clock::now();
  return last_;
}

void LifecycleManagerActiveWatchdog::reset()
{
  dropPending();
  grace_.restart(now());
  last_ = Status{Level::WARN, params_.manager + " no answer yet", {}};
}

}  // namespace guide_robot_supervisor
