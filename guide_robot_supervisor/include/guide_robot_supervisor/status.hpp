#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace guide_robot_supervisor {

/// Уровень результата проверки. Значения совпадают с
/// diagnostic_msgs/DiagnosticStatus, чтобы класть их в диагностику без перевода.
enum class Level : uint8_t { OK = 0, WARN = 1, ERROR = 2, STALE = 3 };

const char * toString(Level level);

struct Status {
  Level level{Level::OK};
  std::string message;
  /// Ключ-значение для diagnostics, порядок сохраняется.
  std::vector<std::pair<std::string, std::string>> values;

  /// «Плохой» результат копит strikes. WARN — никогда: он только для людей.
  bool bad() const { return level == Level::ERROR || level == Level::STALE; }
};

}  // namespace guide_robot_supervisor
