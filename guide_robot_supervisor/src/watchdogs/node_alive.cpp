#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include "guide_robot_supervisor/watchdogs.hpp"

namespace guide_robot_supervisor {

NodeAliveWatchdog::NodeAliveWatchdog(
  std::string name, WatchdogContext & ctx, NodeAliveParams params)
: Watchdog(std::move(name), ctx), params_(std::move(params)), grace_(now(), params_.grace)
{
}

Status NodeAliveWatchdog::check()
{
  std::unordered_set<std::string> present;
  for (const auto & [node_name, ns] :
       node().get_node_graph_interface()->get_node_names_and_namespaces()) {
    std::string fq = ns;
    while (!fq.empty() && fq.back() == '/') fq.pop_back();
    present.insert(fq + "/" + node_name);
  }

  std::string missing;
  for (const auto & wanted : params_.nodes) {
    if (present.count(wanted) == 0) missing += (missing.empty() ? "" : ", ") + wanted;
  }
  if (!missing.empty()) {
    const Level level = grace_.open(now()) ? Level::WARN : Level::ERROR;
    return Status{level, "missing: " + missing, {}};
  }
  return Status{Level::OK, std::to_string(params_.nodes.size()) + " nodes present", {}};
}

void NodeAliveWatchdog::reset()
{
  grace_.restart(now());
}

}  // namespace guide_robot_supervisor
