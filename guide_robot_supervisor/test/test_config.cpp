#include <gtest/gtest.h>
#include <yaml-cpp/yaml.h>

#include <cstdlib>
#include <string>
#include <variant>

#include "guide_robot_supervisor/config.hpp"

using guide_robot_supervisor::Action;
using guide_robot_supervisor::Config;
using guide_robot_supervisor::ConfigError;
using guide_robot_supervisor::dropTopics;
using guide_robot_supervisor::LifecycleManagerActiveParams;
using guide_robot_supervisor::loadConfig;
using guide_robot_supervisor::NodeAliveParams;
using guide_robot_supervisor::parseConfig;
using guide_robot_supervisor::TfEdgeParams;
using guide_robot_supervisor::TopicRateParams;
using guide_robot_supervisor::WatchdogSpec;

namespace {

std::string configPath(const std::string & name)
{
  const char * dir = std::getenv("CONFIG_DIR");
  return std::string(dir != nullptr ? dir : "config") + "/" + name;
}

const WatchdogSpec & watchdog(const Config & cfg, const std::string & name)
{
  for (const auto & w : cfg.watchdogs) {
    if (w.name == name) return w;
  }
  throw std::runtime_error("no watchdog " + name);
}

Config parse(const std::string & text)
{
  return parseConfig(YAML::Load(text));
}

/// Минимальный валидный конфиг; body дописывается в конец секции supervisor.
const char * const kWatchdogs = R"(
  watchdogs:
    - name: scan
      class: TopicRateWatchdog
      params: {topics: [/scan]}
)";

void expectError(const std::string & text, const std::string & fragment)
{
  try {
    parse(text);
    FAIL() << "expected ConfigError containing '" << fragment << "'";
  } catch (const ConfigError & exc) {
    EXPECT_NE(std::string(exc.what()).find(fragment), std::string::npos)
      << "message: " << exc.what();
  }
}

}  // namespace

// ------------------------------------------------------------- real configs --

TEST(RealConfig, HardwareConfigParses)
{
  const Config cfg = loadConfig(configPath("supervisor.yaml"));

  ASSERT_EQ(cfg.groups.size(), 8U);
  EXPECT_EQ(cfg.groups.front().name, "safety");
  EXPECT_EQ(cfg.groups.front().manager, "/lifecycle_manager_safety");
  EXPECT_EQ(cfg.groups.front().precondition_timeout, 60.0);
  EXPECT_FALSE(cfg.groups.front().optional);
  EXPECT_EQ(cfg.groups.back().name, "llm");
  EXPECT_TRUE(cfg.groups.back().optional);
  ASSERT_EQ(cfg.watchdogs.size(), 6U);

  const auto & scan = watchdog(cfg, "scan_rate");
  const auto & scan_params = std::get<TopicRateParams>(scan.params);
  EXPECT_EQ(scan_params.topics.size(), 3U);
  EXPECT_EQ(scan_params.msg_type, "sensor_msgs/msg/LaserScan");
  EXPECT_TRUE(scan_params.sensor_qos);
  EXPECT_EQ(scan_params.min_rate, 5.0);
  EXPECT_EQ(scan.policy.on_error, Action::PAUSE);
  EXPECT_EQ(scan.policy.target.value_or(""), "navigation");

  const auto & tf_odom = std::get<TfEdgeParams>(watchdog(cfg, "tf_odom").params);
  EXPECT_EQ(tf_odom.parent, "odom");
  EXPECT_EQ(tf_odom.child, "base_footprint");
  EXPECT_EQ(watchdog(cfg, "tf_odom").policy.period, 0.5);

  EXPECT_TRUE(std::holds_alternative<NodeAliveParams>(watchdog(cfg, "control_stack").params));
  EXPECT_EQ(watchdog(cfg, "control_stack").policy.on_error, Action::SHUTDOWN);
  EXPECT_FALSE(watchdog(cfg, "control_stack").policy.target.has_value());

  const auto & alive = std::get<LifecycleManagerActiveParams>(watchdog(cfg, "safety_alive").params);
  EXPECT_EQ(alive.manager, "/lifecycle_manager_safety");
}

TEST(RealConfig, SlamConfigParses)
{
  const Config cfg = loadConfig(configPath("supervisor_slam.yaml"));
  EXPECT_FALSE(cfg.groups.empty());
  EXPECT_TRUE(std::holds_alternative<NodeAliveParams>(watchdog(cfg, "slam_alive").params));
}

TEST(RealConfig, OneLidarModeDropsOnlyScanRight)
{
  for (const char * name : {"supervisor.yaml", "supervisor_slam.yaml"}) {
    Config cfg = loadConfig(configPath(name));
    const auto sonar_before = std::get<TopicRateParams>(watchdog(cfg, "sonar_rate").params).topics;

    dropTopics(cfg, {"/scan_right"});

    const auto & scan = std::get<TopicRateParams>(watchdog(cfg, "scan_rate").params);
    EXPECT_EQ(scan.topics, (std::vector<std::string>{"/scan", "/scan_left"})) << name;
    EXPECT_EQ(std::get<TopicRateParams>(watchdog(cfg, "sonar_rate").params).topics, sonar_before);
  }
}

// ---------------------------------------------------------------- dropTopics --

TEST(DropTopics, UnknownTopicIsANoop)
{
  Config cfg = loadConfig(configPath("supervisor.yaml"));
  dropTopics(cfg, {"/scan_nope"});
  EXPECT_EQ(std::get<TopicRateParams>(watchdog(cfg, "scan_rate").params).topics.size(), 3U);
}

TEST(DropTopics, LastTopicIsAnError)
{
  Config cfg = parse(std::string("supervisor:\n  groups: []\n") + kWatchdogs);
  EXPECT_THROW(dropTopics(cfg, {"/scan"}), ConfigError);
}

// ----------------------------------------------------------------- defaults --

TEST(Defaults, AreFilledIn)
{
  const Config cfg = parse(R"(
supervisor:
  groups:
    - {name: g, manager: /m}
  watchdogs:
    - {name: rate, class: TopicRateWatchdog, params: {topics: [/a], min_rate: 3.0}}
    - {name: tf, class: TfEdgeWatchdog, params: {parent: /map, child: odom}}
    - {name: alive, class: NodeAliveWatchdog, params: {nodes: [controller_manager]}}
)");
  const auto & g = cfg.groups.at(0);
  EXPECT_EQ(g.startup_timeout, 30.0);
  EXPECT_EQ(g.precondition_timeout, 60.0);
  EXPECT_FALSE(g.optional);
  EXPECT_TRUE(g.requires_groups.empty());

  const auto & rate = std::get<TopicRateParams>(watchdog(cfg, "rate").params);
  EXPECT_EQ(rate.warn_rate, 6.0);  // 2 * min_rate
  EXPECT_EQ(rate.window, 10);
  EXPECT_EQ(watchdog(cfg, "rate").policy.on_error, Action::WARN);
  EXPECT_EQ(watchdog(cfg, "rate").policy.debounce, 3);
  EXPECT_TRUE(watchdog(cfg, "rate").policy.recover);

  EXPECT_EQ(std::get<TfEdgeParams>(watchdog(cfg, "tf").params).parent, "map");  // '/' срезан
  EXPECT_EQ(
    std::get<NodeAliveParams>(watchdog(cfg, "alive").params).nodes.at(0), "/controller_manager");
}

TEST(Defaults, PythonClassPathsAreAliases)
{
  const Config cfg = parse(R"(
supervisor:
  groups: []
  watchdogs:
    - name: a
      class: guide_robot_supervisor.watchdogs.tf_raw.TFRawWatchdog
    - name: b
      class: guide_robot_supervisor.watchdogs.node_alive.LifecycleManagerActiveWatchdog
      params: {manager: /m}
)");
  EXPECT_TRUE(std::holds_alternative<TfEdgeParams>(watchdog(cfg, "a").params));
  EXPECT_TRUE(std::holds_alternative<LifecycleManagerActiveParams>(watchdog(cfg, "b").params));
}

// ------------------------------------------------------------------- errors --

TEST(Errors, MissingRoot)
{
  expectError("groups: []", "'supervisor' is missing");
}

TEST(Errors, TypoInGroupKey)
{
  expectError(
    std::string("supervisor:\n  groups:\n    - {name: g, manager: /m, startup_timout: 5}\n") +
      kWatchdogs,
    "groups[g] (line 3): unknown key 'startup_timout'");
}

TEST(Errors, TypoInPolicyKey)
{
  expectError(
    R"(
supervisor:
  groups: []
  watchdogs:
    - {name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}, policy: {on_eror: pause}}
)",
    "watchdogs[w].policy");
}

TEST(Errors, TypoInParamKey)
{
  expectError(
    R"(
supervisor:
  groups: []
  watchdogs:
    - {name: w, class: TfEdgeWatchdog, params: {parent: map, chlid: odom}}
)",
    "unknown key 'chlid'");
}

TEST(Errors, UnknownClass)
{
  expectError(
    R"(
supervisor:
  groups: []
  watchdogs:
    - {name: w, class: guide_robot_supervisor.watchdogs.tf_available.TFWatchdog}
)",
    "unknown class");
}

TEST(Errors, UnknownPrecondition)
{
  expectError(
    std::string("supervisor:\n  groups:\n    - {name: g, manager: /m, preconditions: [scna]}\n") +
      kWatchdogs,
    "unknown watchdog 'scna'");
}

TEST(Errors, UnknownRequires)
{
  expectError(
    "supervisor:\n  groups:\n    - {name: g, manager: /m, requires: [nope]}\n",
    "requires unknown group 'nope'");
}

TEST(Errors, RequiresALaterGroup)
{
  expectError(
    R"(
supervisor:
  groups:
    - {name: nav, manager: /a, requires: [loc]}
    - {name: loc, manager: /b}
)",
    "not listed before it");
}

TEST(Errors, UnknownTarget)
{
  expectError(
    R"(
supervisor:
  groups: [{name: g, manager: /m}]
  watchdogs:
    - {name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}, policy: {target: navigaton}}
)",
    "unknown group 'navigaton'");
}

TEST(Errors, DuplicateNames)
{
  expectError(
    "supervisor:\n  groups:\n    - {name: g, manager: /a}\n    - {name: g, manager: /b}\n",
    "duplicate name");
  expectError(
    R"(
supervisor:
  groups: []
  watchdogs:
    - {name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}}
    - {name: w, class: NodeAliveWatchdog, params: {nodes: [/b]}}
)",
    "duplicate name");
}

TEST(Errors, BadValues)
{
  const std::string head = "supervisor:\n  groups: []\n  watchdogs:\n    - ";
  expectError(
    head +
      "{name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}, policy: {on_error: explode}}",
    "'on_error' must be one of");
  expectError(
    head + "{name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}, policy: {debounce: three}}",
    "'debounce' must be an integer");
  expectError(
    head + "{name: w, class: NodeAliveWatchdog, params: {nodes: [/a]}, policy: {debounce: 0}}",
    "'debounce' must be >= 1");
  expectError(head + "{name: w, class: NodeAliveWatchdog, params: {}}", "'nodes' is required");
  expectError(
    head + "{name: w, class: TopicRateWatchdog, params: {topics: [/a], min_rate: 5, warn_rate: 2}}",
    "'warn_rate' must be >= 'min_rate'");
  expectError(
    head + "{name: w, class: TopicRateWatchdog, params: {topics: [/a], qos: fast}}",
    "'qos' must be");
  expectError(
    head + "{name: w, class: TopicRateWatchdog, params: {topics: /a}}",
    "'topics' must be a list of strings");
  expectError(head + "{name: w, class: LifecycleManagerActiveWatchdog}", "'manager' is required");
  expectError("supervisor:\n  groups:\n    - {name: g}\n", "'manager' is required");
}

TEST(Errors, LoadConfigNamesTheFile)
{
  try {
    loadConfig("/nonexistent/supervisor.yaml");
    FAIL() << "expected ConfigError";
  } catch (const ConfigError & exc) {
    EXPECT_NE(std::string(exc.what()).find("/nonexistent/supervisor.yaml"), std::string::npos);
  }
}
