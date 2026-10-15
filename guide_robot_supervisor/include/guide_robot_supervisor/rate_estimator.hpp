#pragma once

#include <cstddef>
#include <deque>

namespace guide_robot_supervisor {

/// Частота сообщений по скользящему окну меток времени прихода.
/// Без ROS и без часов: время передаёт вызывающий код, поэтому логика
/// тестируется на голых числах.
class RateEstimator
{
public:
  /// window — сколько последних меток хранить (не меньше 2).
  explicit RateEstimator(size_t window);

  void add(double stamp);
  void clear() { stamps_.clear(); }

  /// Частота в Гц на момент now. Если с последнего сообщения прошло больше
  /// 2 / min_rate, возвращает 0: протухший буфер не должен показывать
  /// историческую частоту.
  double rate(double now, double min_rate) const;

private:
  std::deque<double> stamps_;
  size_t window_;
};

}  // namespace guide_robot_supervisor
