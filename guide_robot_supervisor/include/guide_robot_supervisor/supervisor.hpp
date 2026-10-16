#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "diagnostic_msgs/msg/diagnostic_array.hpp"
#include "guide_robot_supervisor/config.hpp"
#include "guide_robot_supervisor/status.hpp"
#include "guide_robot_supervisor/watchdog.hpp"
#include "nav2_msgs/srv/manage_lifecycle_nodes.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_srvs/srv/trigger.hpp"

namespace guide_robot_supervisor {

enum class SupervisorState { INIT, BRINGUP, ACTIVE, DEGRADED, RECOVERING, FAULT, SHUTDOWN };

const char * toString(SupervisorState state);

/// Упорядоченный bring-up групп nav2_lifecycle_manager и watchdog'и с политиками.
///
/// Потоки. Всё состояние супервизора (FSM, группы, слоты watchdog'ов) читает и
/// меняет только рабочий поток. Executor обслуживает подписки watchdog'ов,
/// ответы lifecycle-менеджеров и сервисы; сервисы ~/bringup, ~/shutdown,
/// ~/reset лишь ставят команду в очередь, ответ отправляет рабочий поток,
/// когда команда выполнена. Поэтому два bring-up параллельно невозможны.
///
/// Все lifecycle-менеджеры групп должны быть запущены с autostart: false —
/// порядком владеет супервизор.
class Supervisor : public rclcpp::Node
{
public:
  /// Бросает ConfigError, если конфиг не читается или не проходит проверку.
  explicit Supervisor(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());
  ~Supervisor() override;
  Supervisor(const Supervisor &) = delete;
  Supervisor & operator=(const Supervisor &) = delete;
  Supervisor(Supervisor &&) = delete;
  Supervisor & operator=(Supervisor &&) = delete;

  /// Запустить рабочий поток.
  void start();
  /// Остановить рабочий поток, дождаться его и уничтожить watchdog'и.
  /// Executor к этому моменту должен быть остановлен. Идемпотентно.
  void stop();

  SupervisorState state() const { return state_; }

private:
  using ManageNodes = nav2_msgs::srv::ManageLifecycleNodes;
  using Trigger = std_srvs::srv::Trigger;
  using SteadyClock = std::chrono::steady_clock;

  enum class GroupState { INACTIVE, ACTIVE, PAUSED, FAILED };
  enum class Command { BRINGUP, SHUTDOWN, RESET };

  struct Group {
    GroupSpec spec;
    GroupState state{GroupState::INACTIVE};
    rclcpp::Client<ManageNodes>::SharedPtr client;
  };

  struct Slot {
    std::string name;
    Policy policy;
    std::unique_ptr<Watchdog> impl;
    Status status{Level::WARN, "not checked yet", {}};
    /// Проверялся хотя бы раз с последнего reset: непроверенный
    /// watchdog не может открыть precondition.
    bool checked{false};
    int strikes{0};
    bool latched{false};
    SteadyClock::time_point next_check{};
  };

  struct PendingCommand {
    Command command{Command::BRINGUP};
    std::shared_ptr<rmw_request_id_t> header;
  };

  static const char * toString(GroupState state);

  // рабочий поток
  void run();
  void tick();
  void processCommands();
  /// Подождать d, продолжая публиковать состояние и опрашивать watchdog'и.
  /// false — супервизор останавливается.
  bool idle(SteadyClock::duration d);
  void housekeeping();
  bool stopping() const;
  /// Пришла команда, ради которой стоит прервать bring-up (shutdown/reset), или стоп.
  bool abortRequested();

  // bring-up и действия
  bool bringup();
  bool waitPreconditions(const Group & group);
  bool manage(Group & group, uint8_t command, double timeout);
  void shutdownAll();
  void resetAll();

  // watchdog'и
  void pollWatchdogs(bool force);
  void applyPolicies();
  std::vector<Group *> targets(const Policy & policy);
  void doAction(const Policy & policy);
  void undoAction(const Policy & policy);

  // публикации
  void setState(SupervisorState state);
  void publishState();
  void publishEstop(bool engaged);
  void publishDiagnostics();

  bool autostart_{true};
  std::chrono::nanoseconds loop_period_{};

  std::atomic<SupervisorState> state_{SupervisorState::INIT};
  std::vector<Group> groups_;
  std::unordered_map<std::string, size_t> group_index_;

  rclcpp::CallbackGroup::SharedPtr callback_group_;
  std::unique_ptr<WatchdogContext> watchdog_ctx_;
  std::vector<Slot> slots_;
  std::unordered_map<std::string, size_t> slot_index_;

  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr state_pub_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diag_pub_;
  rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr estop_pub_;
  std::unordered_map<int, rclcpp::Service<Trigger>::SharedPtr> services_;

  SteadyClock::time_point next_state_pub_{};
  SteadyClock::time_point next_diagnostics_{};

  std::mutex queue_mutex_;
  std::condition_variable queue_cv_;
  std::deque<PendingCommand> queue_;
  std::atomic<bool> stop_{false};
  std::thread worker_;
};

}  // namespace guide_robot_supervisor
