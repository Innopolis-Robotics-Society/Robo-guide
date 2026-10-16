#pragma once

#include <memory>
#include <string>

#include "guide_robot_supervisor/config.hpp"
#include "guide_robot_supervisor/status.hpp"
#include "rclcpp/rclcpp.hpp"

namespace guide_robot_supervisor {

class TfEdgeTracker;

/// Что watchdog'и берут у узла супервизора.
///
/// Время жизни: узел переживает контекст и все watchdog'и, а executor должен
/// быть остановлен до уничтожения watchdog'ов — колбэки подписок держат this.
class WatchdogContext
{
public:
  WatchdogContext(rclcpp::Node & node, rclcpp::CallbackGroup::SharedPtr group);

  rclcpp::Node & node() const { return node_; }
  const rclcpp::CallbackGroup::SharedPtr & group() const { return group_; }

  /// Одна подписка на /tf на весь узел, создаётся при первом запросе.
  std::shared_ptr<TfEdgeTracker> tfTracker();

private:
  rclcpp::Node & node_;
  rclcpp::CallbackGroup::SharedPtr group_;
  std::shared_ptr<TfEdgeTracker> tf_tracker_;
};

/// Окно после старта (или reset), в котором отсутствие данных — WARN, а не ERROR.
class GraceWindow
{
public:
  GraceWindow(double start, double length) : start_(start), length_(length) {}
  bool open(double now) const { return now - start_ < length_; }
  void restart(double now) { start_ = now; }

private:
  double start_;
  double length_;
};

/// Общий контракт проверки. check() и reset() зовёт только рабочий поток
/// супервизора; колбэки ROS приходят из executor'а, синхронизация — внутри
/// конкретного watchdog'а.
class Watchdog
{
public:
  Watchdog(std::string name, WatchdogContext & ctx);
  virtual ~Watchdog() = default;
  Watchdog(const Watchdog &) = delete;
  Watchdog & operator=(const Watchdog &) = delete;
  Watchdog(Watchdog &&) = delete;
  Watchdog & operator=(Watchdog &&) = delete;

  const std::string & name() const { return name_; }

  /// Не блокирует: результат строится из уже накопленных данных.
  virtual Status check() = 0;
  /// Забыть накопленное и заново открыть окно grace (после восстановления).
  virtual void reset() = 0;

protected:
  /// Время часов узла (учитывает use_sim_time), с.
  double now() const;
  rclcpp::Node & node() const { return ctx_.node(); }
  const rclcpp::CallbackGroup::SharedPtr & group() const { return ctx_.group(); }
  const rclcpp::Logger & logger() const { return logger_; }

private:
  std::string name_;
  WatchdogContext & ctx_;
  rclcpp::Logger logger_;
};

/// Создать watchdog по разобранной спеке.
std::unique_ptr<Watchdog> makeWatchdog(const WatchdogSpec & spec, WatchdogContext & ctx);

}  // namespace guide_robot_supervisor
