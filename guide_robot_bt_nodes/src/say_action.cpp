// =============================================================================
//  say_action.cpp — Nav2 BT plugin: SayAction
//
//  XML usage:
//    <SayAction text="Внимание, отойдите!" />
//
//  Connects to /say action server (tts_node.py in guide_robot_voice).
//  Sends the text with PRIORITY_SAFETY (200), non-interruptible.
//  Returns SUCCESS when TTS node finishes speaking.
// =============================================================================

#include <memory>
#include <string>

#include "guide_robot_msgs/action/say.hpp"
#include "nav2_behavior_tree/bt_action_node.hpp"

namespace guide_robot_bt_nodes {

// Сокращение для удобства
using Say = guide_robot_msgs::action::Say;

class SayAction : public nav2_behavior_tree::BtActionNode<Say>
{
public:
  // ── Конструктор ────────────────────────────────────────────────────────────
  // xml_tag_name — имя тега в XML ("SayAction")
  // conf         — конфигурация BT-узла (порты, blackboard)
  //
  // Первый аргумент BtActionNode — имя action-сервера в ROS2.
  // "/say" — именно то имя, под которым tts_node.py регистрирует сервер.
  SayAction(const std::string & xml_tag_name, const BT::NodeConfiguration & conf)
  : BtActionNode<Say>("say", xml_tag_name, conf)
  {
  }

  // ── on_tick() вызывается ПЕРЕД отправкой goal ─────────────────────────────
  // Здесь заполняем поля goal_, которые BtActionNode отправит на сервер.
  void on_tick() override
  {
    // Текст берётся из BT-порта "text"
    getInput("text", goal_.text);

    // Безопасностные настройки
    goal_.priority = Say::Goal::PRIORITY_SAFETY;  // 200 — выше диалога и нарратива
    goal_.scope = Say::Goal::SCOPE_SAFETY;  // 3   — прерывает всё кроме такого же
    goal_.interruptible = false;            // не прерывать barge-in'ом
    goal_.max_duration = 10.0f;             // не зависать дольше 10 сек
  }

  // ── Порты BT-узла (то что пишем в XML) ────────────────────────────────────
  // providedBasicPorts() добавляет стандартные порты BtActionNode
  // (server_name, server_timeout) к нашим.
  static BT::PortsList providedPorts()
  {
    return providedBasicPorts({
      BT::InputPort<std::string>("text", "Текст для синтеза речи"),
    });
  }
};

}  // namespace guide_robot_bt_nodes

// ── Регистрация плагина ───────────────────────────────────────────────────────
// bt_navigator загружает .so и вызывает эту функцию.
// "SayAction" — имя которое пишем в XML.
#include "behaviortree_cpp/bt_factory.h"
BT_REGISTER_NODES(factory)
{
  factory.registerNodeType<guide_robot_bt_nodes::SayAction>("SayAction");
}
