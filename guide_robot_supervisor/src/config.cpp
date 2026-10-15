#include "guide_robot_supervisor/config.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <initializer_list>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace guide_robot_supervisor {

namespace {

enum class WatchdogKind { TOPIC_RATE, TF_EDGE, NODE_ALIVE, LIFECYCLE_MANAGER_ACTIVE };

/// `class:` из YAML -> вид watchdog'а. Питоновские dotted-path оставлены
/// алиасами, чтобы старые конфиги читались без правок.
const std::unordered_map<std::string, WatchdogKind> & watchdogClasses()
{
  static const std::unordered_map<std::string, WatchdogKind> classes{
    {"TopicRateWatchdog", WatchdogKind::TOPIC_RATE},
    {"guide_robot_supervisor.watchdogs.topic_rate.TopicRateWatchdog", WatchdogKind::TOPIC_RATE},
    {"TfEdgeWatchdog", WatchdogKind::TF_EDGE},
    {"guide_robot_supervisor.watchdogs.tf_raw.TFRawWatchdog", WatchdogKind::TF_EDGE},
    {"NodeAliveWatchdog", WatchdogKind::NODE_ALIVE},
    {"guide_robot_supervisor.watchdogs.node_alive.NodeAliveWatchdog", WatchdogKind::NODE_ALIVE},
    {"LifecycleManagerActiveWatchdog", WatchdogKind::LIFECYCLE_MANAGER_ACTIVE},
    {"guide_robot_supervisor.watchdogs.node_alive.LifecycleManagerActiveWatchdog",
     WatchdogKind::LIFECYCLE_MANAGER_ACTIVE},
  };
  return classes;
}

std::string at(const std::string & ctx, const YAML::Node & node)
{
  const auto mark = node.Mark();
  if (mark.line < 0) return ctx;
  return ctx + " (line " + std::to_string(mark.line + 1) + ")";
}

[[noreturn]] void fail(const std::string & ctx, const std::string & what)
{
  throw ConfigError(ctx + ": " + what);
}

void expectMap(const YAML::Node & node, const std::string & ctx)
{
  if (!node.IsMap()) fail(at(ctx, node), "expected a mapping");
}

void checkKeys(
  const YAML::Node & map, std::initializer_list<const char *> allowed, const std::string & ctx)
{
  for (const auto & kv : map) {
    const auto key = kv.first.as<std::string>();
    const bool known = std::any_of(
      allowed.begin(), allowed.end(), [&key](const char * name) { return key == name; });
    if (!known) {
      std::string list;
      for (const char * name : allowed) list += std::string(list.empty() ? "" : ", ") + name;
      fail(at(ctx, kv.first), "unknown key '" + key + "' (allowed: " + list + ")");
    }
  }
}

template <typename T>
const char * typeName();
template <>
const char * typeName<std::string>()
{
  return "a string";
}
template <>
const char * typeName<double>()
{
  return "a number";
}
template <>
const char * typeName<int>()
{
  return "an integer";
}
template <>
const char * typeName<bool>()
{
  return "a boolean";
}
template <>
const char * typeName<std::vector<std::string>>()
{
  return "a list of strings";
}

/// Значение ключа или fallback, если ключа нет или он null.
template <typename T>
T read(const YAML::Node & map, const char * key, const T & fallback, const std::string & ctx)
{
  const YAML::Node node = map[key];
  if (!node || node.IsNull()) return fallback;
  try {
    return node.as<T>();
  } catch (const YAML::BadConversion &) {
    fail(at(ctx, node), std::string("'") + key + "' must be " + typeName<T>());
  }
}

template <typename T>
T need(const YAML::Node & map, const char * key, const std::string & ctx)
{
  const YAML::Node node = map[key];
  if (!node || node.IsNull()) fail(at(ctx, map), std::string("'") + key + "' is required");
  return read<T>(map, key, T{}, ctx);
}

void positive(double value, const char * key, const std::string & ctx)
{
  if (!(value > 0.0)) fail(ctx, std::string("'") + key + "' must be > 0");
}

void nonNegative(double value, const char * key, const std::string & ctx)
{
  if (!(value >= 0.0)) fail(ctx, std::string("'") + key + "' must be >= 0");
}

std::string stripSlash(std::string frame)
{
  frame.erase(0, frame.find_first_not_of('/'));
  return frame;
}

void noDuplicates(const std::vector<std::string> & items, const char * key, const std::string & ctx)
{
  std::set<std::string> seen;
  for (const auto & item : items) {
    if (!seen.insert(item).second) fail(ctx, std::string("duplicate '") + item + "' in " + key);
  }
}

// ------------------------------------------------------------------ params --

TopicRateParams parseTopicRate(const YAML::Node & p, const std::string & ctx)
{
  checkKeys(p, {"topics", "msg_type", "qos", "min_rate", "warn_rate", "window", "grace"}, ctx);
  TopicRateParams out;
  out.topics = need<std::vector<std::string>>(p, "topics", ctx);
  if (out.topics.empty()) fail(ctx, "'topics' must not be empty");
  noDuplicates(out.topics, "topics", ctx);
  out.msg_type = read<std::string>(p, "msg_type", "", ctx);
  const auto qos = read<std::string>(p, "qos", "sensor", ctx);
  if (qos != "sensor" && qos != "default") fail(ctx, "'qos' must be 'sensor' or 'default'");
  out.sensor_qos = qos == "sensor";
  out.min_rate = read<double>(p, "min_rate", 1.0, ctx);
  positive(out.min_rate, "min_rate", ctx);
  out.warn_rate = read<double>(p, "warn_rate", out.min_rate * 2.0, ctx);
  if (out.warn_rate < out.min_rate) fail(ctx, "'warn_rate' must be >= 'min_rate'");
  out.window = read<int>(p, "window", 10, ctx);
  if (out.window < 2) fail(ctx, "'window' must be >= 2");
  out.grace = read<double>(p, "grace", 10.0, ctx);
  nonNegative(out.grace, "grace", ctx);
  return out;
}

TfEdgeParams parseTfEdge(const YAML::Node & p, const std::string & ctx)
{
  checkKeys(p, {"parent", "child", "max_age", "grace"}, ctx);
  TfEdgeParams out;
  out.parent = stripSlash(read<std::string>(p, "parent", out.parent, ctx));
  out.child = stripSlash(read<std::string>(p, "child", out.child, ctx));
  if (out.parent.empty() || out.child.empty()) fail(ctx, "'parent' and 'child' must not be empty");
  if (out.parent == out.child) fail(ctx, "'parent' and 'child' must differ");
  out.max_age = read<double>(p, "max_age", out.max_age, ctx);
  positive(out.max_age, "max_age", ctx);
  out.grace = read<double>(p, "grace", out.grace, ctx);
  nonNegative(out.grace, "grace", ctx);
  return out;
}

NodeAliveParams parseNodeAlive(const YAML::Node & p, const std::string & ctx)
{
  checkKeys(p, {"nodes", "grace"}, ctx);
  NodeAliveParams out;
  out.nodes = need<std::vector<std::string>>(p, "nodes", ctx);
  if (out.nodes.empty()) fail(ctx, "'nodes' must not be empty");
  for (auto & node : out.nodes) {
    if (node.empty() || node.front() != '/') node.insert(0, "/");
  }
  out.grace = read<double>(p, "grace", out.grace, ctx);
  nonNegative(out.grace, "grace", ctx);
  return out;
}

LifecycleManagerActiveParams parseLifecycleManagerActive(
  const YAML::Node & p, const std::string & ctx)
{
  checkKeys(p, {"manager", "timeout", "grace"}, ctx);
  LifecycleManagerActiveParams out;
  out.manager = need<std::string>(p, "manager", ctx);
  if (out.manager.empty()) fail(ctx, "'manager' must not be empty");
  out.timeout = read<double>(p, "timeout", out.timeout, ctx);
  positive(out.timeout, "timeout", ctx);
  out.grace = read<double>(p, "grace", out.grace, ctx);
  nonNegative(out.grace, "grace", ctx);
  return out;
}

// ------------------------------------------------------------------ policy --

Action parseAction(const std::string & name, const std::string & ctx)
{
  static const std::unordered_map<std::string, Action> actions{
    {"warn", Action::WARN},         {"pause", Action::PAUSE}, {"reset", Action::RESET},
    {"shutdown", Action::SHUTDOWN}, {"estop", Action::ESTOP},
  };
  const auto it = actions.find(name);
  if (it == actions.end()) {
    fail(ctx, "'on_error' must be one of warn, pause, reset, shutdown, estop, got '" + name + "'");
  }
  return it->second;
}

Policy parsePolicy(const YAML::Node & p, const std::string & ctx)
{
  Policy out;
  if (!p || p.IsNull()) return out;
  expectMap(p, ctx);
  checkKeys(p, {"on_error", "target", "debounce", "recover", "period"}, ctx);
  out.on_error = parseAction(read<std::string>(p, "on_error", "warn", ctx), ctx);
  const auto target = read<std::string>(p, "target", "", ctx);
  if (!target.empty()) out.target = target;
  out.debounce = read<int>(p, "debounce", out.debounce, ctx);
  if (out.debounce < 1) fail(ctx, "'debounce' must be >= 1");
  out.recover = read<bool>(p, "recover", out.recover, ctx);
  out.period = read<double>(p, "period", out.period, ctx);
  positive(out.period, "period", ctx);
  return out;
}

// ---------------------------------------------------------------- sections --

WatchdogSpec parseWatchdog(const YAML::Node & w, size_t index)
{
  std::string ctx = "watchdogs[" + std::to_string(index) + "]";
  expectMap(w, ctx);
  WatchdogSpec out;
  out.name = need<std::string>(w, "name", ctx);
  ctx = "watchdogs[" + out.name + "]";
  checkKeys(w, {"name", "class", "params", "policy"}, ctx);

  const auto cls = need<std::string>(w, "class", ctx);
  const auto kind = watchdogClasses().find(cls);
  if (kind == watchdogClasses().end()) fail(at(ctx, w["class"]), "unknown class '" + cls + "'");

  // w["params"] у отсутствующего ключа — «невалидный» узел, присваивать ему нельзя
  const YAML::Node given = w["params"];
  const YAML::Node params = (!given || given.IsNull()) ? YAML::Node(YAML::NodeType::Map) : given;
  const std::string pctx = ctx + ".params";
  expectMap(params, pctx);
  switch (kind->second) {
    case WatchdogKind::TOPIC_RATE:
      out.params = parseTopicRate(params, pctx);
      break;
    case WatchdogKind::TF_EDGE:
      out.params = parseTfEdge(params, pctx);
      break;
    case WatchdogKind::NODE_ALIVE:
      out.params = parseNodeAlive(params, pctx);
      break;
    case WatchdogKind::LIFECYCLE_MANAGER_ACTIVE:
      out.params = parseLifecycleManagerActive(params, pctx);
      break;
  }
  out.policy = parsePolicy(w["policy"], ctx + ".policy");
  return out;
}

GroupSpec parseGroup(const YAML::Node & g, size_t index)
{
  std::string ctx = "groups[" + std::to_string(index) + "]";
  expectMap(g, ctx);
  GroupSpec out;
  out.name = need<std::string>(g, "name", ctx);
  ctx = "groups[" + out.name + "]";
  checkKeys(
    g,
    {"name", "manager", "requires", "preconditions", "startup_timeout", "precondition_timeout",
     "optional"},
    ctx);
  out.manager = need<std::string>(g, "manager", ctx);
  if (out.manager.empty()) fail(ctx, "'manager' must not be empty");
  out.requires_groups = read<std::vector<std::string>>(g, "requires", {}, ctx);
  noDuplicates(out.requires_groups, "requires", ctx);
  out.preconditions = read<std::vector<std::string>>(g, "preconditions", {}, ctx);
  noDuplicates(out.preconditions, "preconditions", ctx);
  out.startup_timeout = read<double>(g, "startup_timeout", out.startup_timeout, ctx);
  positive(out.startup_timeout, "startup_timeout", ctx);
  out.precondition_timeout = read<double>(g, "precondition_timeout", out.precondition_timeout, ctx);
  positive(out.precondition_timeout, "precondition_timeout", ctx);
  out.optional = read<bool>(g, "optional", out.optional, ctx);
  return out;
}

YAML::Node sequence(const YAML::Node & root, const char * key, bool required)
{
  const YAML::Node node = root[key];
  if (!node || node.IsNull()) {
    if (required) fail(std::string("supervisor"), std::string("'") + key + "' is required");
    return YAML::Node(YAML::NodeType::Sequence);
  }
  if (!node.IsSequence()) fail(at(std::string("supervisor.") + key, node), "expected a list");
  return node;
}

/// Ссылки между секциями: requires, preconditions, target.
void crossCheck(const Config & cfg)
{
  std::set<std::string> watchdogs;
  for (const auto & w : cfg.watchdogs) {
    if (!watchdogs.insert(w.name).second) fail("watchdogs[" + w.name + "]", "duplicate name");
  }

  std::set<std::string> earlier;
  std::set<std::string> all;
  for (const auto & g : cfg.groups) all.insert(g.name);
  for (const auto & g : cfg.groups) {
    const std::string ctx = "groups[" + g.name + "]";
    if (earlier.count(g.name) != 0) fail(ctx, "duplicate name");
    for (const auto & dep : g.requires_groups) {
      if (all.count(dep) == 0) fail(ctx, "requires unknown group '" + dep + "'");
      // bring-up — один проход по списку: зависимость, стоящая ниже,
      // к моменту проверки ещё не поднята, и группа гарантированно падает.
      if (earlier.count(dep) == 0) {
        fail(
          ctx, "requires '" + dep + "', which is not listed before it (bring-up is in list order)");
      }
    }
    for (const auto & pre : g.preconditions) {
      if (watchdogs.count(pre) == 0)
        fail(ctx, "precondition refers to unknown watchdog '" + pre + "'");
    }
    earlier.insert(g.name);
  }

  for (const auto & w : cfg.watchdogs) {
    if (w.policy.target && all.count(*w.policy.target) == 0) {
      fail(
        "watchdogs[" + w.name + "].policy",
        "target refers to unknown group '" + *w.policy.target + "'");
    }
  }
}

}  // namespace

const char * toString(Action action)
{
  switch (action) {
    case Action::WARN:
      return "warn";
    case Action::PAUSE:
      return "pause";
    case Action::RESET:
      return "reset";
    case Action::SHUTDOWN:
      return "shutdown";
    case Action::ESTOP:
      return "estop";
  }
  return "?";
}

Config parseConfig(const YAML::Node & root)
{
  if (!root.IsMap() || !root["supervisor"]) fail("config", "top-level key 'supervisor' is missing");
  const YAML::Node sup = root["supervisor"];
  expectMap(sup, "supervisor");

  Config cfg;
  // YAML-исключения (например, не-строковый ключ) тоже превращаем в ConfigError.
  try {
    checkKeys(sup, {"groups", "watchdogs"}, "supervisor");
    const YAML::Node watchdogs = sequence(sup, "watchdogs", false);
    for (size_t i = 0; i < watchdogs.size(); ++i)
      cfg.watchdogs.push_back(parseWatchdog(watchdogs[i], i));
    const YAML::Node groups = sequence(sup, "groups", true);
    for (size_t i = 0; i < groups.size(); ++i) cfg.groups.push_back(parseGroup(groups[i], i));
  } catch (const YAML::Exception & exc) {
    throw ConfigError(std::string("config: ") + exc.what());
  }
  crossCheck(cfg);
  return cfg;
}

Config loadConfig(const std::string & path)
{
  YAML::Node root;
  try {
    root = YAML::LoadFile(path);
  } catch (const YAML::Exception & exc) {
    throw ConfigError(path + ": " + exc.what());
  }
  try {
    return parseConfig(root);
  } catch (const ConfigError & exc) {
    throw ConfigError(path + ": " + exc.what());
  }
}

void dropTopics(Config & config, const std::vector<std::string> & topics)
{
  for (auto & w : config.watchdogs) {
    auto * params = std::get_if<TopicRateParams>(&w.params);
    if (params == nullptr) continue;
    auto & list = params->topics;
    list.erase(
      std::remove_if(
        list.begin(), list.end(),
        [&topics](const std::string & t) {
          return std::find(topics.begin(), topics.end(), t) != topics.end();
        }),
      list.end());
    if (list.empty()) fail("watchdogs[" + w.name + "]", "no topics left after dropping");
  }
}

}  // namespace guide_robot_supervisor
