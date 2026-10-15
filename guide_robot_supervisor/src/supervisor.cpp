#include "guide_robot_supervisor/supervisor.hpp"

#include <algorithm>
#include <chrono>
#include <future>
#include <string>
#include <utility>
#include <vector>

namespace guide_robot_supervisor {

namespace {

using namespace std::chrono_literals;

/// Шаг ожидания внутри idle(): реакция на стоп и шаг опроса будущих ответов.
constexpr auto kSlice = 20ms;
constexpr auto kDiagnosticsPeriod = 1s;
constexpr auto kPreconditionRetry = 500ms;

// Таймауты действий политик, как в Python-версии.
constexpr double kPauseTimeout = 10.0;
constexpr double kResumeTimeout = 10.0;
constexpr double kResetTimeout = 15.0;
constexpr double kShutdownTimeout = 10.0;

std::chrono::nanoseconds toDuration(double seconds)
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::duration<double>(seconds));
}

std::string join(const std::vector<std::string> & items)
{
  std::string out;
  for (const auto & item : items) out += (out.empty() ? "" : ", ") + item;
  return out;
}

const char * commandName(uint8_t command)
{
  using Req = nav2_msgs::srv::ManageLifecycleNodes::Request;
  switch (command) {
    case Req::STARTUP:
      return "STARTUP";
    case Req::PAUSE:
      return "PAUSE";
    case Req::RESUME:
      return "RESUME";
    case Req::RESET:
      return "RESET";
    case Req::SHUTDOWN:
      return "SHUTDOWN";
    default:
      return "?";
  }
}

}  // namespace

const char * toString(SupervisorState state)
{
  switch (state) {
    case SupervisorState::INIT:
      return "INIT";
    case SupervisorState::BRINGUP:
      return "BRINGUP";
    case SupervisorState::ACTIVE:
      return "ACTIVE";
    case SupervisorState::DEGRADED:
      return "DEGRADED";
    case SupervisorState::RECOVERING:
      return "RECOVERING";
    case SupervisorState::FAULT:
      return "FAULT";
    case SupervisorState::SHUTDOWN:
      return "SHUTDOWN";
  }
  return "?";
}

const char * Supervisor::toString(GroupState state)
{
  switch (state) {
    case GroupState::INACTIVE:
      return "INACTIVE";
    case GroupState::ACTIVE:
      return "ACTIVE";
    case GroupState::PAUSED:
      return "PAUSED";
    case GroupState::FAILED:
      return "FAILED";
  }
  return "?";
}

// ------------------------------------------------------------------- setup --

Supervisor::Supervisor(const rclcpp::NodeOptions & options) : Node("supervisor", options)
{
  const auto config_file = declare_parameter<std::string>("config_file", "");
  autostart_ = declare_parameter<bool>("autostart", true);
  loop_period_ = toDuration(declare_parameter<double>("loop_period", 0.2));
  const auto estop_topic = declare_parameter<std::string>("estop_topic", "/supervisor/estop");
  // Режим с одним лидаром: launch передаёт ["/scan_right"].
  const auto drop_topics =
    declare_parameter<std::vector<std::string>>("drop_topics", std::vector<std::string>{});

  if (config_file.empty()) throw ConfigError("parameter 'config_file' is required");
  Config config = loadConfig(config_file);
  if (!drop_topics.empty()) dropTopics(config, drop_topics);

  callback_group_ = create_callback_group(rclcpp::CallbackGroupType::Reentrant);

  for (auto & spec : config.groups) {
    Group group;
    group.client = create_client<ManageNodes>(
      spec.manager + "/manage_nodes", rmw_qos_profile_services_default, callback_group_);
    group.spec = std::move(spec);
    group_index_[group.spec.name] = groups_.size();
    groups_.push_back(std::move(group));
  }

  watchdog_ctx_ = std::make_unique<WatchdogContext>(*this, callback_group_);
  slots_.reserve(config.watchdogs.size());
  for (const auto & spec : config.watchdogs) {
    Slot slot;
    slot.name = spec.name;
    slot.policy = spec.policy;
    slot.impl = makeWatchdog(spec, *watchdog_ctx_);
    slot_index_[slot.name] = slots_.size();
    slots_.push_back(std::move(slot));
  }

  state_pub_ = create_publisher<std_msgs::msg::String>("~/state", 10);
  diag_pub_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>("~/diagnostics", 10);
  // latched: подписчик, поднявшийся позже (twist_mux, operator_ui), сразу
  // узнаёт текущее значение, а не ждёт следующего фронта
  estop_pub_ =
    create_publisher<std_msgs::msg::Bool>(estop_topic, rclcpp::QoS(1).reliable().transient_local());

  const auto add_service = [this](const char * name, Command command) {
    services_[static_cast<int>(command)] = create_service<Trigger>(
      name,
      [this, command](
        const std::shared_ptr<rmw_request_id_t> header,
        const std::shared_ptr<Trigger::Request> /*request*/) {
        {
          const std::lock_guard<std::mutex> lock(queue_mutex_);
          queue_.push_back(PendingCommand{command, header});
        }
        queue_cv_.notify_all();
      },
      rmw_qos_profile_services_default, callback_group_);
  };
  add_service("~/bringup", Command::BRINGUP);
  add_service("~/shutdown", Command::SHUTDOWN);
  add_service("~/reset", Command::RESET);

  publishEstop(false);

  std::vector<std::string> group_names;
  for (const auto & g : groups_) group_names.push_back(g.spec.name);
  std::vector<std::string> watchdog_names;
  for (const auto & s : slots_) watchdog_names.push_back(s.name);
  RCLCPP_INFO(
    get_logger(), "supervisor up: groups=[%s], watchdogs=[%s]", join(group_names).c_str(),
    join(watchdog_names).c_str());
}

Supervisor::~Supervisor()
{
  stop();
}

void Supervisor::start()
{
  if (!worker_.joinable() && !stop_) worker_ = std::thread([this] { run(); });
}

void Supervisor::stop()
{
  {
    const std::lock_guard<std::mutex> lock(queue_mutex_);
    stop_ = true;
  }
  queue_cv_.notify_all();
  if (worker_.joinable()) worker_.join();
  // watchdog'и — после рабочего потока и остановленного executor'а:
  // их колбэки держат this
  slots_.clear();
  slot_index_.clear();
  watchdog_ctx_.reset();
}

// ----------------------------------------------------------- worker thread --

void Supervisor::run()
{
  while (!stopping()) {
    try {
      processCommands();
      tick();
    } catch (const std::exception & exc) {  // супервизор не должен умирать
      RCLCPP_ERROR(get_logger(), "tick failed: %s", exc.what());
    }
    idle(loop_period_);
  }
}

void Supervisor::tick()
{
  pollWatchdogs(false);
  const SupervisorState state = state_;
  if (state == SupervisorState::INIT) {
    if (autostart_) bringup();
  } else if (state == SupervisorState::ACTIVE || state == SupervisorState::DEGRADED) {
    applyPolicies();
  }
}

void Supervisor::processCommands()
{
  while (!stopping()) {
    PendingCommand pending;
    {
      const std::lock_guard<std::mutex> lock(queue_mutex_);
      if (queue_.empty()) return;
      pending = queue_.front();
      queue_.pop_front();
    }

    Trigger::Response response;
    switch (pending.command) {
      case Command::BRINGUP:
        RCLCPP_INFO(get_logger(), "~/bringup requested");
        response.success = bringup();
        response.message = guide_robot_supervisor::toString(state_.load());
        break;
      case Command::SHUTDOWN:
        RCLCPP_INFO(get_logger(), "~/shutdown requested");
        shutdownAll();
        response.success = true;
        response.message = guide_robot_supervisor::toString(state_.load());
        break;
      case Command::RESET:
        RCLCPP_INFO(get_logger(), "~/reset requested");
        resetAll();
        response.success = true;
        response.message = "reset";
        break;
    }
    services_.at(static_cast<int>(pending.command))->send_response(*pending.header, response);
  }
}

bool Supervisor::stopping() const
{
  return stop_ || !rclcpp::ok();
}

bool Supervisor::abortRequested()
{
  if (stopping()) return true;
  const std::lock_guard<std::mutex> lock(queue_mutex_);
  return std::any_of(queue_.begin(), queue_.end(), [](const PendingCommand & c) {
    return c.command != Command::BRINGUP;
  });
}

bool Supervisor::idle(SteadyClock::duration d)
{
  const auto deadline = SteadyClock::now() + d;
  while (!stopping()) {
    housekeeping();
    const auto now = SteadyClock::now();
    if (now >= deadline) return true;
    std::unique_lock<std::mutex> lock(queue_mutex_);
    queue_cv_.wait_for(lock, std::min<SteadyClock::duration>(deadline - now, kSlice), [this] {
      return stop_.load();
    });
  }
  return false;
}

void Supervisor::housekeeping()
{
  // И во время долгого bring-up или вызова менеджера: watchdog'и опрашиваются,
  // состояние и диагностика публикуются.
  pollWatchdogs(false);
  const auto now = SteadyClock::now();
  if (now >= next_state_pub_) {
    publishState();
    next_state_pub_ = now + loop_period_;
  }
  if (now >= next_diagnostics_) {
    publishDiagnostics();
    next_diagnostics_ = now + kDiagnosticsPeriod;
  }
}

// ---------------------------------------------------------------- bring-up --

bool Supervisor::bringup()
{
  setState(SupervisorState::BRINGUP);
  for (auto & group : groups_) {
    const auto & spec = group.spec;
    if (abortRequested()) {
      RCLCPP_WARN(get_logger(), "bring-up interrupted before [%s]", spec.name.c_str());
      return false;
    }
    // Повторный bring-up (после FAULT или ручной) поднимает только то, что не поднято.
    if (group.state == GroupState::ACTIVE || group.state == GroupState::PAUSED) continue;

    std::vector<std::string> unmet;
    for (const auto & dep : spec.requires_groups) {
      if (groups_[group_index_.at(dep)].state != GroupState::ACTIVE) unmet.push_back(dep);
    }
    if (!unmet.empty()) {
      RCLCPP_ERROR(
        get_logger(), "[%s] dependencies not active: %s", spec.name.c_str(), join(unmet).c_str());
      if (spec.optional) continue;
      setState(SupervisorState::FAULT);
      return false;
    }

    if (!waitPreconditions(group)) {
      if (abortRequested()) return false;
      if (spec.optional) continue;
      setState(SupervisorState::FAULT);
      return false;
    }

    RCLCPP_INFO(get_logger(), "[%s] STARTUP -> %s", spec.name.c_str(), spec.manager.c_str());
    if (manage(group, ManageNodes::Request::STARTUP, spec.startup_timeout)) {
      group.state = GroupState::ACTIVE;
    } else {
      group.state = GroupState::FAILED;
      if (!spec.optional) {
        setState(SupervisorState::FAULT);
        return false;
      }
    }
  }

  publishEstop(false);
  setState(SupervisorState::ACTIVE);
  return true;
}

bool Supervisor::waitPreconditions(const Group & group)
{
  const auto & spec = group.spec;
  if (spec.preconditions.empty()) return true;

  const auto deadline = SteadyClock::now() + toDuration(spec.precondition_timeout);
  std::vector<std::string> bad;
  while (true) {
    pollWatchdogs(true);
    bad.clear();
    for (const auto & name : spec.preconditions) {
      const Slot & slot = slots_[slot_index_.at(name)];
      if (!slot.checked || slot.status.level != Level::OK) {
        bad.push_back(name + " (" + slot.status.message + ")");
      }
    }
    if (bad.empty()) return true;
    if (SteadyClock::now() >= deadline || abortRequested() || !idle(kPreconditionRetry)) break;
  }
  RCLCPP_ERROR(
    get_logger(), "[%s] preconditions not met: %s", spec.name.c_str(), join(bad).c_str());
  return false;
}

bool Supervisor::manage(Group & group, uint8_t command, double timeout)
{
  const auto & spec = group.spec;
  const auto budget = toDuration(timeout);

  auto deadline = SteadyClock::now() + budget;
  while (!group.client->service_is_ready()) {
    if (SteadyClock::now() >= deadline || !idle(kSlice)) {
      RCLCPP_ERROR(
        get_logger(), "[%s] %s: %s/manage_nodes not available", spec.name.c_str(),
        commandName(command), spec.manager.c_str());
      return false;
    }
  }

  auto request = std::make_shared<ManageNodes::Request>();
  request->command = command;
  auto call = group.client->async_send_request(request);
  deadline = SteadyClock::now() + budget;
  while (call.future.wait_for(0s) != std::future_status::ready) {
    if (SteadyClock::now() >= deadline || !idle(kSlice)) {
      // поздний ответ не должен висеть в клиенте
      group.client->remove_pending_request(call);
      RCLCPP_ERROR(
        get_logger(), "[%s] %s on %s: no answer in %.1f s", spec.name.c_str(), commandName(command),
        spec.manager.c_str(), timeout);
      return false;
    }
  }
  if (!call.future.get()->success) {
    RCLCPP_ERROR(
      get_logger(), "[%s] %s on %s failed", spec.name.c_str(), commandName(command),
      spec.manager.c_str());
    return false;
  }
  return true;
}

void Supervisor::shutdownAll()
{
  setState(SupervisorState::SHUTDOWN);
  for (auto it = groups_.rbegin(); it != groups_.rend(); ++it) {
    if (it->state == GroupState::ACTIVE || it->state == GroupState::PAUSED) {
      manage(*it, ManageNodes::Request::SHUTDOWN, kShutdownTimeout);
      it->state = GroupState::INACTIVE;
    }
  }
}

void Supervisor::resetAll()
{
  shutdownAll();
  for (auto & slot : slots_) {
    slot.strikes = 0;
    slot.latched = false;
    slot.checked = false;
    slot.impl->reset();
  }
  for (auto & group : groups_) group.state = GroupState::INACTIVE;
  setState(SupervisorState::INIT);
}

// --------------------------------------------------------------- watchdogs --

void Supervisor::pollWatchdogs(bool force)
{
  const auto now = SteadyClock::now();
  for (auto & slot : slots_) {
    if (!force && now < slot.next_check) continue;
    slot.next_check = now + toDuration(slot.policy.period);
    try {
      slot.status = slot.impl->check();
    } catch (const std::exception & exc) {
      slot.status = Status{Level::ERROR, std::string("check raised: ") + exc.what(), {}};
    }
    slot.checked = true;
    slot.strikes = slot.status.bad() ? slot.strikes + 1 : 0;
  }
}

void Supervisor::applyPolicies()
{
  bool degraded = false;
  for (auto & slot : slots_) {
    const Policy & policy = slot.policy;
    const bool tripped = slot.strikes >= policy.debounce;

    if (tripped && !slot.latched) {
      slot.latched = true;
      RCLCPP_ERROR(
        get_logger(), "watchdog '%s' tripped (%s) -> action '%s'", slot.name.c_str(),
        slot.status.message.c_str(), guide_robot_supervisor::toString(policy.on_error));
      doAction(policy);
      // shutdown погасил всё и перевёл в FAULT: остальные политики уже не
      // применяются, иначе чей-нибудь undo вывел бы FSM из FAULT
      if (state_ == SupervisorState::FAULT) return;
    } else if (!tripped && slot.latched && policy.recover && slot.status.level == Level::OK) {
      slot.latched = false;
      RCLCPP_INFO(get_logger(), "watchdog '%s' recovered -> resuming", slot.name.c_str());
      slot.impl->reset();
      undoAction(policy);
    }
    degraded = degraded || slot.latched;
  }
  setState(degraded ? SupervisorState::DEGRADED : SupervisorState::ACTIVE);
}

std::vector<Supervisor::Group *> Supervisor::targets(const Policy & policy)
{
  std::vector<Group *> out;
  if (policy.target) {
    out.push_back(&groups_[group_index_.at(*policy.target)]);
  } else {
    for (auto it = groups_.rbegin(); it != groups_.rend(); ++it) out.push_back(&*it);
  }
  return out;
}

void Supervisor::doAction(const Policy & policy)
{
  switch (policy.on_error) {
    case Action::WARN:
      return;
    case Action::ESTOP:
      publishEstop(true);
      return;
    case Action::SHUTDOWN:
      publishEstop(true);
      shutdownAll();
      setState(SupervisorState::FAULT);
      return;
    case Action::PAUSE:
    case Action::RESET:
      break;
  }
  for (Group * group : targets(policy)) {
    if (group->state != GroupState::ACTIVE) continue;
    if (policy.on_error == Action::PAUSE) {
      manage(*group, ManageNodes::Request::PAUSE, kPauseTimeout);
      group->state = GroupState::PAUSED;
    } else {
      manage(*group, ManageNodes::Request::RESET, kResetTimeout);
      group->state = GroupState::INACTIVE;
    }
  }
}

void Supervisor::undoAction(const Policy & policy)
{
  switch (policy.on_error) {
    case Action::WARN:
    case Action::SHUTDOWN:  // из FAULT выводит только ~/reset или ~/bringup
      return;
    case Action::ESTOP: {
      const bool any_latched =
        std::any_of(slots_.begin(), slots_.end(), [](const Slot & s) { return s.latched; });
      if (!any_latched) publishEstop(false);
      return;
    }
    case Action::PAUSE:
    case Action::RESET:
      break;
  }
  setState(SupervisorState::RECOVERING);
  for (Group * group : targets(policy)) {
    if (policy.on_error == Action::PAUSE && group->state == GroupState::PAUSED) {
      manage(*group, ManageNodes::Request::RESUME, kResumeTimeout);
      group->state = GroupState::ACTIVE;
    } else if (policy.on_error == Action::RESET && group->state == GroupState::INACTIVE) {
      if (manage(*group, ManageNodes::Request::STARTUP, group->spec.startup_timeout)) {
        group->state = GroupState::ACTIVE;
      }
    }
  }
}

// -------------------------------------------------------------- publishing --

void Supervisor::setState(SupervisorState state)
{
  const SupervisorState previous = state_.exchange(state);
  if (previous != state) {
    RCLCPP_INFO(
      get_logger(), "state: %s -> %s", guide_robot_supervisor::toString(previous),
      guide_robot_supervisor::toString(state));
  }
  publishState();
}

void Supervisor::publishState()
{
  std_msgs::msg::String msg;
  msg.data = guide_robot_supervisor::toString(state_.load());
  state_pub_->publish(msg);
}

void Supervisor::publishEstop(bool engaged)
{
  std_msgs::msg::Bool msg;
  msg.data = engaged;
  estop_pub_->publish(msg);
}

void Supervisor::publishDiagnostics()
{
  using diagnostic_msgs::msg::DiagnosticStatus;
  using diagnostic_msgs::msg::KeyValue;

  const auto kv = [](std::string key, std::string value) {
    KeyValue out;
    out.key = std::move(key);
    out.value = std::move(value);
    return out;
  };

  diagnostic_msgs::msg::DiagnosticArray msg;
  msg.header.stamp = now();

  const SupervisorState state = state_;
  DiagnosticStatus top;
  top.name = "supervisor";
  top.hardware_id = "robo-guide";
  top.level = state == SupervisorState::ACTIVE  ? DiagnosticStatus::OK
              : state == SupervisorState::FAULT ? DiagnosticStatus::ERROR
                                                : DiagnosticStatus::WARN;
  top.message = guide_robot_supervisor::toString(state);
  for (const auto & group : groups_)
    top.values.push_back(kv(group.spec.name, toString(group.state)));
  msg.status.push_back(top);

  for (const auto & slot : slots_) {
    DiagnosticStatus st;
    st.name = "supervisor/" + slot.name;
    st.hardware_id = "robo-guide";
    st.level = static_cast<uint8_t>(slot.status.level);
    st.message = slot.status.message;
    for (const auto & [key, value] : slot.status.values) st.values.push_back(kv(key, value));
    st.values.push_back(kv("strikes", std::to_string(slot.strikes)));
    st.values.push_back(kv("latched", slot.latched ? "True" : "False"));
    msg.status.push_back(st);
  }
  diag_pub_->publish(msg);
}

}  // namespace guide_robot_supervisor
