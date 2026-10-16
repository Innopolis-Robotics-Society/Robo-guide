#include "guide_robot_supervisor/status.hpp"

namespace guide_robot_supervisor {

const char * toString(Level level)
{
  switch (level) {
    case Level::OK:
      return "OK";
    case Level::WARN:
      return "WARN";
    case Level::ERROR:
      return "ERROR";
    case Level::STALE:
      return "STALE";
  }
  return "?";
}

}  // namespace guide_robot_supervisor
