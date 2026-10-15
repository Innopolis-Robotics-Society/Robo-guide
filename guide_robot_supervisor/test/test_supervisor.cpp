#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <fstream>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "guide_robot_supervisor/config.hpp"
#include "guide_robot_supervisor/supervisor.hpp"
#include "nav2_msgs/srv/manage_lifecycle_nodes.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/range.hpp"
#include "std_msgs/msg/bool.hpp"
#include "std_srvs/srv/trigger.hpp"

using namespace std::chrono_literals;
using guide_robot_supervisor::ConfigError;
using guide_robot_supervisor::Supervisor;
using guide_robot_supervisor::SupervisorState;
using ManageNodes = nav2_msgs::srv::ManageLifecycleNodes;
using Trigger = std_srvs::srv::Trigger;

namespace {

/// Журнал вызовов всех фейковых менеджеров: "a:STARTUP", "b:PAUSE", ...
class CallLog
{
public:
  void add(const std::string & entry)
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    calls_.push_back(entry);
  }
  std::vector<std::string> calls() const
  {
    const std::lock_guard<std::mutex> lock(mutex_);
    return calls_;
  }
  bool contains(const std::string & entry) const
  {
    const auto all = calls();
    return std::find(all.begin(), all.end(), entry) != all.end();
  }

private:
  mutable std::mutex mutex_;
  std::vector<std::string> calls_;
};

const char * commandName(uint8_t command)
{
  switch (command) {
    case ManageNodes::Request::STARTUP:
      return "STARTUP";
    case ManageNodes::Request::PAUSE:
      return "PAUSE";
    case ManageNodes::Request::RESUME:
      return "RESUME";
    case ManageNodes::Request::RESET:
      return "RESET";
    case ManageNodes::Request::SHUTDOWN:
      return "SHUTDOWN";
    default:
      return "?";
  }
}

/// nav2_lifecycle_manager понарошку: только <name>/manage_nodes.
class FakeManager
{
public:
  FakeManager(rclcpp::Node & node, const std::string & name, CallLog & log)
  {
    srv_ = node.create_service<ManageNodes>(
      "/" + name + "/manage_nodes", [this, name, &log](
                                      const std::shared_ptr<ManageNodes::Request> req,
                                      std::shared_ptr<ManageNodes::Response> res) {
        log.add(name + ":" + commandName(req->command));
        res->success = !fail;
      });
  }
  std::atomic<bool> fail{false};

private:
  rclcpp::Service<ManageNodes>::SharedPtr srv_;
};

class SupervisorTest : public ::testing::Test
{
protected:
  void SetUp() override
  {
    stand_ = std::make_shared<rclcpp::Node>("sup_test_stand");
    executor_.add_node(stand_);
    spinner_ = std::thread([this] { executor_.spin(); });
  }

  void TearDown() override
  {
    executor_.cancel();
    spinner_.join();
    if (supervisor_) supervisor_->stop();
    supervisor_.reset();
  }

  void manager(const std::string & name)
  {
    managers_.push_back(std::make_unique<FakeManager>(*stand_, name, log_));
  }

  /// Записать конфиг во временный файл и поднять супервизор с ним.
  void launch(const std::string & yaml, bool autostart = true, std::vector<std::string> drop = {})
  {
    const std::string path = ::testing::TempDir() + "sup_test_" +
                             ::testing::UnitTest::GetInstance()->current_test_info()->name() +
                             ".yaml";
    std::ofstream(path) << yaml;

    rclcpp::NodeOptions options;
    // свой namespace: сервисы и топики не пересекутся с живым супервизором
    options.arguments({"--ros-args", "-r", "__ns:=/sup_test"});
    options.parameter_overrides({
      {"config_file", path},
      {"autostart", autostart},
      {"loop_period", 0.05},
      {"estop_topic", "/sup_test/estop"},
    });
    if (!drop.empty()) options.parameter_overrides().emplace_back("drop_topics", drop);
    supervisor_ = std::make_shared<Supervisor>(options);
    executor_.add_node(supervisor_);
    supervisor_->start();
  }

  bool waitState(SupervisorState wanted, std::chrono::milliseconds timeout = 10s)
  {
    return waitUntil([&] { return supervisor_->state() == wanted; }, timeout);
  }

  static bool waitUntil(const std::function<bool()> & done, std::chrono::milliseconds timeout = 10s)
  {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (!done()) {
      if (std::chrono::steady_clock::now() >= deadline) return false;
      std::this_thread::sleep_for(20ms);
    }
    return true;
  }

  Trigger::Response::SharedPtr call(const std::string & service)
  {
    auto client = stand_->create_client<Trigger>("/sup_test/supervisor/" + service);
    EXPECT_TRUE(client->wait_for_service(5s));
    auto future = client->async_send_request(std::make_shared<Trigger::Request>());
    EXPECT_EQ(future.wait_for(30s), std::future_status::ready);
    return future.get();
  }

  /// Значение estop, которое увидит подписчик, пришедший сейчас (latched).
  std::optional<bool> lateEstop()
  {
    std::atomic<int> value{-1};
    auto sub = stand_->create_subscription<std_msgs::msg::Bool>(
      "/sup_test/estop", rclcpp::QoS(1).reliable().transient_local(),
      [&value](const std_msgs::msg::Bool & msg) { value = msg.data ? 1 : 0; });
    waitUntil([&] { return value >= 0; }, 3s);
    if (value < 0) return std::nullopt;
    return value == 1;
  }

  /// Паблишер Range с включаемым потоком сообщений, 50 Гц.
  std::shared_ptr<std::atomic<bool>> publishRange(const std::string & topic)
  {
    auto on = std::make_shared<std::atomic<bool>>(true);
    auto pub = stand_->create_publisher<sensor_msgs::msg::Range>(topic, rclcpp::SensorDataQoS());
    timers_.push_back(stand_->create_wall_timer(20ms, [pub, on] {
      if (*on) pub->publish(sensor_msgs::msg::Range());
    }));
    return on;
  }

  CallLog log_;
  rclcpp::Node::SharedPtr stand_;
  std::vector<std::unique_ptr<FakeManager>> managers_;
  std::vector<rclcpp::TimerBase::SharedPtr> timers_;
  std::shared_ptr<Supervisor> supervisor_;
  rclcpp::executors::SingleThreadedExecutor executor_;
  std::thread spinner_;
};

const char * const kTwoGroups = R"(
supervisor:
  groups:
    - {name: a, manager: /sup_a}
    - {name: missing, manager: /sup_missing, startup_timeout: 0.3, optional: true}
    - {name: b, manager: /sup_b, requires: [a]}
)";

}  // namespace

TEST_F(SupervisorTest, BringsGroupsUpInOrderAndSkipsMissingOptional)
{
  manager("sup_a");
  manager("sup_b");
  launch(kTwoGroups);

  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
  EXPECT_EQ(log_.calls(), (std::vector<std::string>{"sup_a:STARTUP", "sup_b:STARTUP"}));
  EXPECT_EQ(lateEstop(), std::optional<bool>(false));
}

TEST_F(SupervisorTest, FailedRequiredGroupIsAFault)
{
  manager("sup_a");
  manager("sup_b");
  managers_[0]->fail = true;
  launch(kTwoGroups);

  ASSERT_TRUE(waitState(SupervisorState::FAULT));
  std::this_thread::sleep_for(200ms);
  EXPECT_EQ(log_.calls(), (std::vector<std::string>{"sup_a:STARTUP"}));
}

TEST_F(SupervisorTest, AutostartOffWaitsForBringupService)
{
  manager("sup_a");
  manager("sup_b");
  launch(kTwoGroups, /*autostart=*/false);

  std::this_thread::sleep_for(500ms);
  EXPECT_EQ(supervisor_->state(), SupervisorState::INIT);
  EXPECT_TRUE(log_.calls().empty());

  const auto res = call("bringup");
  EXPECT_TRUE(res->success);
  EXPECT_EQ(res->message, "ACTIVE");

  // повторный bring-up уже поднятого не дёргает менеджеры
  EXPECT_TRUE(call("bringup")->success);
  EXPECT_EQ(log_.calls().size(), 2U);
}

TEST_F(SupervisorTest, ShutdownAndResetServices)
{
  manager("sup_a");
  manager("sup_b");
  launch(kTwoGroups);
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));

  const auto down = call("shutdown");
  EXPECT_TRUE(down->success);
  EXPECT_EQ(down->message, "SHUTDOWN");
  EXPECT_EQ(
    log_.calls(), (std::vector<std::string>{
                    "sup_a:STARTUP", "sup_b:STARTUP", "sup_b:SHUTDOWN", "sup_a:SHUTDOWN"}));

  // reset -> INIT -> autostart поднимает заново
  EXPECT_EQ(call("reset")->message, "reset");
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
  EXPECT_EQ(log_.calls().size(), 6U);
  EXPECT_EQ(log_.calls().back(), "sup_b:STARTUP");
}

TEST_F(SupervisorTest, PreconditionGatesStartupAndPolicyPausesAndResumes)
{
  manager("sup_a");
  manager("sup_nav");
  launch(R"(
supervisor:
  groups:
    - {name: a, manager: /sup_a, preconditions: [range], precondition_timeout: 10.0}
    - {name: nav, manager: /sup_nav, requires: [a]}
  watchdogs:
    - name: range
      class: TopicRateWatchdog
      params: {topics: [/sup_test/range], msg_type: sensor_msgs/msg/Range, min_rate: 5.0,
               warn_rate: 10.0, grace: 1.0}
      policy: {on_error: pause, target: nav, debounce: 2, period: 0.1}
)");

  // данных нет — STARTUP не уходит
  std::this_thread::sleep_for(700ms);
  EXPECT_EQ(supervisor_->state(), SupervisorState::BRINGUP);
  EXPECT_TRUE(log_.calls().empty());

  auto range_on = publishRange("/sup_test/range");
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
  EXPECT_EQ(log_.calls(), (std::vector<std::string>{"sup_a:STARTUP", "sup_nav:STARTUP"}));

  // датчик умолк -> pause nav, DEGRADED
  *range_on = false;
  ASSERT_TRUE(waitState(SupervisorState::DEGRADED));
  EXPECT_TRUE(log_.contains("sup_nav:PAUSE"));
  EXPECT_FALSE(log_.contains("sup_a:PAUSE"));

  // вернулся -> resume, ACTIVE
  *range_on = true;
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
  EXPECT_EQ(log_.calls().back(), "sup_nav:RESUME");
}

TEST_F(SupervisorTest, PreconditionTimeoutIsAFault)
{
  manager("sup_a");
  launch(R"(
supervisor:
  groups:
    - {name: a, manager: /sup_a, preconditions: [range], precondition_timeout: 0.5}
  watchdogs:
    - name: range
      class: TopicRateWatchdog
      params: {topics: [/sup_test/silent], msg_type: sensor_msgs/msg/Range, grace: 0.0}
)");
  ASSERT_TRUE(waitState(SupervisorState::FAULT));
  EXPECT_TRUE(log_.calls().empty());
}

TEST_F(SupervisorTest, DropTopicsParameterRemovesTopicFromPrecondition)
{
  manager("sup_a");
  auto on = publishRange("/sup_test/left");
  launch(
    R"(
supervisor:
  groups:
    - {name: a, manager: /sup_a, preconditions: [scan], precondition_timeout: 10.0}
  watchdogs:
    - name: scan
      class: TopicRateWatchdog
      params: {topics: [/sup_test/left, /sup_test/right], msg_type: sensor_msgs/msg/Range,
               min_rate: 5.0, warn_rate: 5.0, grace: 0.0}
)",
    true, {"/sup_test/right"});
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
}

TEST_F(SupervisorTest, ShutdownPolicyFaultsAndLatchesEstop)
{
  manager("sup_a");
  auto victim = std::make_shared<rclcpp::Node>("sup_test_victim");
  launch(R"(
supervisor:
  groups:
    - {name: a, manager: /sup_a}
  watchdogs:
    - name: control_stack
      class: NodeAliveWatchdog
      params: {nodes: [/sup_test_victim], grace: 0.0}
      policy: {on_error: shutdown, debounce: 2, period: 0.1}
)");
  ASSERT_TRUE(waitState(SupervisorState::ACTIVE));
  std::this_thread::sleep_for(500ms);
  EXPECT_EQ(supervisor_->state(), SupervisorState::ACTIVE);  // узел жив — ничего не происходит

  victim.reset();
  ASSERT_TRUE(waitState(SupervisorState::FAULT));
  EXPECT_EQ(log_.calls().back(), "sup_a:SHUTDOWN");
  EXPECT_EQ(lateEstop(), std::optional<bool>(true));

  // из FAULT сам не выходит
  std::this_thread::sleep_for(500ms);
  EXPECT_EQ(supervisor_->state(), SupervisorState::FAULT);
}

TEST_F(SupervisorTest, BadConfigThrowsWithContext)
{
  const std::string path = ::testing::TempDir() + "sup_test_bad.yaml";
  std::ofstream(path) << "supervisor:\n  groups:\n    - {name: a, manager: /m, requires: [nav]}\n";
  rclcpp::NodeOptions options;
  options.parameter_overrides({{"config_file", path}});
  try {
    Supervisor node(options);
    FAIL() << "expected ConfigError";
  } catch (const ConfigError & exc) {
    const std::string what = exc.what();
    EXPECT_NE(what.find(path), std::string::npos) << what;
    EXPECT_NE(what.find("groups[a]"), std::string::npos) << what;
  }
}

int main(int argc, char ** argv)
{
  ::testing::InitGoogleTest(&argc, argv);
  rclcpp::init(argc, argv);
  const int result = RUN_ALL_TESTS();
  rclcpp::shutdown();
  return result;
}
