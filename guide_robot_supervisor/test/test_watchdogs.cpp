#include <gtest/gtest.h>

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>
#include <string>
#include <thread>

#include "guide_robot_supervisor/watchdogs.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/range.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "tf2_msgs/msg/tf_message.hpp"

using namespace std::chrono_literals;
using guide_robot_supervisor::Level;
using guide_robot_supervisor::Status;
using guide_robot_supervisor::Watchdog;
using guide_robot_supervisor::WatchdogContext;

namespace gs = guide_robot_supervisor;

namespace {

/// Узел-«супервизор» с watchdog'ами и узел-«стенд», который публикует данные.
/// Оба крутятся в одном executor'е в фоне, check() зовётся из потока теста,
/// как из рабочего потока супервизора.
class WatchdogTest : public ::testing::Test
{
protected:
  void SetUp() override
  {
    node_ = std::make_shared<rclcpp::Node>("wd_test_supervisor");
    stand_ = std::make_shared<rclcpp::Node>("wd_test_stand");
    group_ = node_->create_callback_group(rclcpp::CallbackGroupType::Reentrant);
    ctx_ = std::make_unique<WatchdogContext>(*node_, group_);
    executor_.add_node(node_);
    executor_.add_node(stand_);
    spinner_ = std::thread([this] { executor_.spin(); });
  }

  void TearDown() override
  {
    executor_.cancel();
    spinner_.join();
    // watchdog'и — после остановки executor'а (их колбэки держат this)
    watchdogs_.clear();
    ctx_.reset();
  }

  template <class T, class Params>
  Watchdog & make(const std::string & name, Params params)
  {
    watchdogs_.push_back(std::make_unique<T>(name, *ctx_, std::move(params)));
    return *watchdogs_.back();
  }

  /// Звать check() каждые 50 мс, пока результат не удовлетворит done.
  static Status waitUntil(
    Watchdog & wd, const std::function<bool(const Status &)> & done,
    std::chrono::milliseconds timeout = 5s)
  {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    Status status = wd.check();
    while (!done(status) && std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(50ms);
      status = wd.check();
    }
    return status;
  }

  static Status waitFor(Watchdog & wd, Level wanted, std::chrono::milliseconds timeout = 5s)
  {
    return waitUntil(
      wd, [wanted](const Status & s) { return s.level == wanted; }, timeout);
  }

  rclcpp::Node::SharedPtr node_;
  rclcpp::Node::SharedPtr stand_;
  rclcpp::CallbackGroup::SharedPtr group_;
  std::unique_ptr<WatchdogContext> ctx_;
  std::vector<std::unique_ptr<Watchdog>> watchdogs_;
  rclcpp::executors::SingleThreadedExecutor executor_;
  std::thread spinner_;
};

}  // namespace

// ---------------------------------------------------------------- TopicRate --

TEST_F(WatchdogTest, TopicRateAutodetectsTypeAndTracksRate)
{
  auto pub =
    stand_->create_publisher<sensor_msgs::msg::Range>("/wd_test/range", rclcpp::SensorDataQoS());
  std::atomic<bool> publishing{true};
  auto timer = stand_->create_wall_timer(20ms, [&] {
    if (publishing) pub->publish(sensor_msgs::msg::Range());
  });

  gs::TopicRateParams params;
  params.topics = {"/wd_test/range"};  // msg_type не задан -> из графа
  params.min_rate = 5.0;
  params.warn_rate = 10.0;
  params.grace = 0.0;
  auto & wd = make<gs::TopicRateWatchdog>("rate", params);

  const Status ok = waitFor(wd, Level::OK);
  ASSERT_EQ(ok.level, Level::OK) << ok.message;
  ASSERT_EQ(ok.values.size(), 1U);
  EXPECT_EQ(ok.values[0].first, "/wd_test/range");

  publishing = false;
  const Status dead = waitFor(wd, Level::ERROR);
  EXPECT_EQ(dead.level, Level::ERROR) << dead.message;
  EXPECT_EQ(dead.message, "no data: /wd_test/range");
}

TEST_F(WatchdogTest, TopicRateWithoutPublisherWaitsInGraceThenFails)
{
  gs::TopicRateParams params;
  params.topics = {"/wd_test/silent"};
  params.msg_type = "sensor_msgs/msg/Range";
  params.grace = 0.3;
  auto & wd = make<gs::TopicRateWatchdog>("silent", params);

  const Status first = wd.check();
  EXPECT_EQ(first.level, Level::WARN);
  EXPECT_EQ(first.message, "waiting for /wd_test/silent");
  EXPECT_EQ(waitFor(wd, Level::ERROR, 2s).level, Level::ERROR);

  wd.reset();  // reset снова открывает grace
  EXPECT_EQ(wd.check().level, Level::WARN);
}

TEST_F(WatchdogTest, TopicRateBadTypeDoesNotThrow)
{
  gs::TopicRateParams params;
  params.topics = {"/wd_test/bad_type"};
  params.msg_type = "no_such_pkg/msg/Nope";
  params.grace = 0.0;
  auto & wd = make<gs::TopicRateWatchdog>("bad_type", params);
  EXPECT_EQ(wd.check().level, Level::ERROR);
}

// ------------------------------------------------------------------- TfEdge --

TEST_F(WatchdogTest, TfEdgeSeesDirectEdgeAndGoesStale)
{
  auto pub = stand_->create_publisher<tf2_msgs::msg::TFMessage>("/tf", rclcpp::QoS(100));
  std::atomic<bool> publishing{true};
  auto timer = stand_->create_wall_timer(20ms, [&] {
    if (!publishing) return;
    tf2_msgs::msg::TFMessage msg;
    msg.transforms.resize(1);
    msg.transforms[0].header.frame_id = "/wd_odom";  // ведущий '/' не мешает
    msg.transforms[0].child_frame_id = "wd_base";
    pub->publish(msg);
  });

  gs::TfEdgeParams edge{"wd_odom", "wd_base", 0.3, 0.0};
  gs::TfEdgeParams never{"wd_map", "wd_odom", 0.3, 0.0};
  gs::TfEdgeParams reversed{"wd_base", "wd_odom", 0.3, 0.0};
  auto & wd = make<gs::TfEdgeWatchdog>("edge", edge);
  auto & wd_never = make<gs::TfEdgeWatchdog>("never", never);
  auto & wd_reversed = make<gs::TfEdgeWatchdog>("reversed", reversed);

  const Status ok = waitFor(wd, Level::OK);
  ASSERT_EQ(ok.level, Level::OK) << ok.message;
  EXPECT_EQ(ok.message, "wd_odom -> wd_base");

  const Status missing = wd_never.check();
  EXPECT_EQ(missing.level, Level::ERROR);
  EXPECT_EQ(missing.message, "wd_map -> wd_odom unavailable: never seen on /tf");
  EXPECT_EQ(wd_reversed.check().level, Level::ERROR);  // только прямое ребро

  publishing = false;
  const Status stale = waitFor(wd, Level::STALE);
  EXPECT_EQ(stale.level, Level::STALE) << stale.message;
}

// ---------------------------------------------------------------- NodeAlive --

TEST_F(WatchdogTest, NodeAliveFindsNodesInGraph)
{
  auto & present = make<gs::NodeAliveWatchdog>(
    "present", gs::NodeAliveParams{{"/wd_test_stand", "/wd_test_supervisor"}, 0.0});
  const Status ok = waitFor(present, Level::OK);
  EXPECT_EQ(ok.level, Level::OK) << ok.message;
  EXPECT_EQ(ok.message, "2 nodes present");

  auto & missing =
    make<gs::NodeAliveWatchdog>("missing", gs::NodeAliveParams{{"/wd_test_stand", "/nope"}, 0.0});
  const Status err = missing.check();
  EXPECT_EQ(err.level, Level::ERROR);
  EXPECT_EQ(err.message, "missing: /nope");

  auto & graceful = make<gs::NodeAliveWatchdog>("graceful", gs::NodeAliveParams{{"/nope"}, 100.0});
  EXPECT_EQ(graceful.check().level, Level::WARN);
}

// --------------------------------------------------- LifecycleManagerActive --

TEST_F(WatchdogTest, LifecycleManagerActiveFollowsTheService)
{
  std::atomic<bool> active{true};
  auto srv = stand_->create_service<std_srvs::srv::Trigger>(
    "/wd_manager/is_active",
    [&](
      const std::shared_ptr<std_srvs::srv::Trigger::Request> /*req*/,
      std::shared_ptr<std_srvs::srv::Trigger::Response> res) { res->success = active; });

  auto & wd = make<gs::LifecycleManagerActiveWatchdog>(
    "alive", gs::LifecycleManagerActiveParams{"/wd_manager", 1.0, 0.0});

  const Status ok = waitFor(wd, Level::OK);
  ASSERT_EQ(ok.level, Level::OK) << ok.message;

  active = false;
  const Status inactive = waitFor(wd, Level::ERROR);
  EXPECT_EQ(inactive.level, Level::ERROR);
  EXPECT_EQ(inactive.message, "/wd_manager inactive");

  // Процесс менеджера умер: сервиса нет вовсе. Раньше это был вечный WARN.
  active = true;
  srv.reset();
  const Status gone = waitUntil(
    wd, [](const Status & s) { return s.message.find("not available") != std::string::npos; });
  EXPECT_EQ(gone.level, Level::ERROR);
  EXPECT_EQ(gone.message, "/wd_manager/is_active not available");
}

TEST_F(WatchdogTest, LifecycleManagerActiveTimesOutOnSilentServer)
{
  // Сервер принимает запрос и никогда не отвечает.
  auto srv = stand_->create_service<std_srvs::srv::Trigger>(
    "/wd_mute/is_active", [](
                            const std::shared_ptr<rmw_request_id_t> /*header*/,
                            const std::shared_ptr<std_srvs::srv::Trigger::Request> /*req*/) {});

  auto & wd = make<gs::LifecycleManagerActiveWatchdog>(
    "mute", gs::LifecycleManagerActiveParams{"/wd_mute", 0.2, 0.0});
  const Status stale = waitFor(wd, Level::STALE);
  EXPECT_EQ(stale.level, Level::STALE);
  EXPECT_EQ(stale.message, "/wd_mute did not answer");
}

TEST_F(WatchdogTest, LifecycleManagerActiveMissingServiceInGraceIsWarn)
{
  auto & wd = make<gs::LifecycleManagerActiveWatchdog>(
    "none", gs::LifecycleManagerActiveParams{"/wd_none", 1.0, 100.0});
  EXPECT_EQ(wd.check().level, Level::WARN);
}

// ------------------------------------------------------------------ factory --

TEST_F(WatchdogTest, FactoryBuildsEveryKind)
{
  const std::vector<gs::WatchdogSpec> specs{
    {"a", gs::TopicRateParams{{"/wd_test/f"}, "sensor_msgs/msg/Range"}, {}},
    {"b", gs::TfEdgeParams{}, {}},
    {"c", gs::NodeAliveParams{{"/x"}}, {}},
    {"d", gs::LifecycleManagerActiveParams{"/wd_f"}, {}},
  };
  for (const auto & spec : specs) {
    watchdogs_.push_back(gs::makeWatchdog(spec, *ctx_));
    EXPECT_EQ(watchdogs_.back()->name(), spec.name);
  }
  EXPECT_NE(dynamic_cast<gs::TopicRateWatchdog *>(watchdogs_[0].get()), nullptr);
  EXPECT_NE(dynamic_cast<gs::TfEdgeWatchdog *>(watchdogs_[1].get()), nullptr);
  EXPECT_NE(dynamic_cast<gs::NodeAliveWatchdog *>(watchdogs_[2].get()), nullptr);
  EXPECT_NE(dynamic_cast<gs::LifecycleManagerActiveWatchdog *>(watchdogs_[3].get()), nullptr);
}

int main(int argc, char ** argv)
{
  ::testing::InitGoogleTest(&argc, argv);
  rclcpp::init(argc, argv);
  const int result = RUN_ALL_TESTS();
  rclcpp::shutdown();
  return result;
}
