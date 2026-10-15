#pragma once

#include <chrono>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "guide_robot_supervisor/config.hpp"
#include "guide_robot_supervisor/rate_estimator.hpp"
#include "guide_robot_supervisor/watchdog.hpp"
#include "rclcpp/rclcpp.hpp"
#include "std_srvs/srv/trigger.hpp"
#include "tf2_msgs/msg/tf_message.hpp"

namespace guide_robot_supervisor {

/// Частота публикации набора топиков; все должны быть живы.
///
/// Подписка generic: сообщение не десериализуется, нужен только момент
/// прихода. Тип берётся из msg_type или из графа; если паблишера ещё нет,
/// подписка создаётся лениво на следующих check().
class TopicRateWatchdog : public Watchdog
{
public:
  TopicRateWatchdog(std::string name, WatchdogContext & ctx, TopicRateParams params);
  Status check() override;
  void reset() override;

private:
  struct Topic {
    std::string name;
    RateEstimator rate;
    rclcpp::GenericSubscription::SharedPtr sub;
    bool error_logged{false};
  };

  void trySubscribe(size_t index);

  TopicRateParams params_;
  rclcpp::QoS qos_;
  GraceWindow grace_;
  /// Размер фиксирован в конструкторе: колбэки держат индекс.
  std::vector<Topic> topics_;
  /// Защищает topics_[i].rate (колбэки executor'а против check()).
  std::mutex mutex_;
};

/// Общая подписка на /tf: когда последний раз приходило прямое ребро parent -> child.
/// Время — момент приёма по часам узла, а не header.stamp: AMCL датирует
/// трансформ будущим (transform_tolerance), «жив ли источник» честнее по приёму.
class TfEdgeTracker
{
public:
  TfEdgeTracker(rclcpp::Node & node, const rclcpp::CallbackGroup::SharedPtr & group);

  /// Зарегистрировать ребро, вернуть его номер. Кадры без ведущего '/'.
  size_t watch(const std::string & parent, const std::string & child);
  std::optional<double> lastSeen(size_t edge) const;
  void forget(size_t edge);

private:
  struct Edge {
    std::string parent;
    std::string child;
    std::optional<double> last_seen;
  };

  void onTf(const tf2_msgs::msg::TFMessage & msg);

  rclcpp::Clock::SharedPtr clock_;
  mutable std::mutex mutex_;
  std::vector<Edge> edges_;
  rclcpp::Subscription<tf2_msgs::msg::TFMessage>::SharedPtr sub_;
};

/// Прямое ребро TF публикуется на /tf и не старше max_age.
/// Только /tf (не /tf_static) и только прямое ребро, не цепочка.
class TfEdgeWatchdog : public Watchdog
{
public:
  TfEdgeWatchdog(std::string name, WatchdogContext & ctx, TfEdgeParams params);
  Status check() override;
  void reset() override;

private:
  TfEdgeParams params_;
  std::shared_ptr<TfEdgeTracker> tracker_;
  size_t edge_;
  GraceWindow grace_;
};

/// Узлы зарегистрированы в графе ROS. Это не проверка отзывчивости:
/// зависший, но зарегистрированный узел здесь живой.
class NodeAliveWatchdog : public Watchdog
{
public:
  NodeAliveWatchdog(std::string name, WatchdogContext & ctx, NodeAliveParams params);
  Status check() override;
  void reset() override;

private:
  NodeAliveParams params_;
  GraceWindow grace_;
};

/// <manager>/is_active отвечает success. Ловит развал группы по bond'у.
///
/// Не блокирует: check() забирает ответ на предыдущий запрос и отправляет
/// следующий, так что результат запаздывает на один период. Пропавший сервис
/// после окна grace — ERROR: падение менеджера целиком тоже должно срабатывать.
class LifecycleManagerActiveWatchdog : public Watchdog
{
public:
  LifecycleManagerActiveWatchdog(
    std::string name, WatchdogContext & ctx, LifecycleManagerActiveParams params);
  ~LifecycleManagerActiveWatchdog() override;
  LifecycleManagerActiveWatchdog(const LifecycleManagerActiveWatchdog &) = delete;
  LifecycleManagerActiveWatchdog & operator=(const LifecycleManagerActiveWatchdog &) = delete;
  LifecycleManagerActiveWatchdog(LifecycleManagerActiveWatchdog &&) = delete;
  LifecycleManagerActiveWatchdog & operator=(LifecycleManagerActiveWatchdog &&) = delete;

  Status check() override;
  void reset() override;

private:
  using Client = rclcpp::Client<std_srvs::srv::Trigger>;

  void dropPending();

  LifecycleManagerActiveParams params_;
  Client::SharedPtr client_;
  std::optional<Client::FutureAndRequestId> pending_;
  std::chrono::steady_clock::time_point sent_at_;
  GraceWindow grace_;
  Status last_;
};

}  // namespace guide_robot_supervisor
