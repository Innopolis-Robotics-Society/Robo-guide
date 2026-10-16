#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <variant>
#include <vector>

namespace YAML {
class Node;
}

namespace guide_robot_supervisor {

/// Ошибка конфига. Сообщение всегда начинается с места ошибки
/// («watchdogs[scan_rate].policy: ...»), чтобы опечатка в YAML не
/// превращалась в голый traceback.
class ConfigError : public std::runtime_error
{
public:
  using std::runtime_error::runtime_error;
};

enum class Action { WARN, PAUSE, RESET, SHUTDOWN, ESTOP };

const char * toString(Action action);

struct Policy {
  Action on_error{Action::WARN};
  /// Имя группы; пусто — все группы.
  std::optional<std::string> target;
  /// Сколько плохих проверок подряд до срабатывания.
  int debounce{3};
  /// Отменить действие, когда проверка снова OK.
  bool recover{true};
  /// Период проверки, с.
  double period{1.0};
};

struct GroupSpec {
  std::string name;
  /// Узел nav2_lifecycle_manager, например /lifecycle_manager_safety.
  std::string manager;
  std::vector<std::string> requires_groups;
  std::vector<std::string> preconditions;
  double startup_timeout{30.0};
  double precondition_timeout{60.0};
  bool optional{false};
};

// ---------------------------------------------------------------- watchdogs --

struct TopicRateParams {
  std::vector<std::string> topics;
  /// Пусто — тип берётся из графа по первому паблишеру.
  std::string msg_type;
  /// true — SensorDataQoS (BEST_EFFORT), false — системный по умолчанию.
  bool sensor_qos{true};
  double min_rate{1.0};
  double warn_rate{2.0};
  int window{10};
  double grace{10.0};
};

struct TfEdgeParams {
  std::string parent{"map"};
  std::string child{"base_link"};
  double max_age{2.0};
  double grace{15.0};
};

struct NodeAliveParams {
  std::vector<std::string> nodes;
  double grace{15.0};
};

struct LifecycleManagerActiveParams {
  std::string manager;
  double timeout{2.0};
  double grace{15.0};
};

using WatchdogParams =
  std::variant<TopicRateParams, TfEdgeParams, NodeAliveParams, LifecycleManagerActiveParams>;

struct WatchdogSpec {
  std::string name;
  WatchdogParams params;
  Policy policy;
};

struct Config {
  /// Порядок = порядок bring-up.
  std::vector<GroupSpec> groups;
  std::vector<WatchdogSpec> watchdogs;
};

/// Разобрать документ целиком (корневой ключ `supervisor`) и проверить
/// ссылки между группами и watchdog'ами. Бросает ConfigError.
Config parseConfig(const YAML::Node & root);
Config loadConfig(const std::string & path);

/// Убрать топики из всех TopicRate-watchdog'ов (режим с одним лидаром).
/// Бросает ConfigError, если у watchdog'а не осталось ни одного топика.
void dropTopics(Config & config, const std::vector<std::string> & topics);

}  // namespace guide_robot_supervisor
