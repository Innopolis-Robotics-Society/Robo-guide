#include "guide_robot_supervisor/rate_estimator.hpp"

#include <algorithm>

namespace guide_robot_supervisor {

RateEstimator::RateEstimator(size_t window) : window_(std::max<size_t>(window, 2)) {}

void RateEstimator::add(double stamp)
{
  stamps_.push_back(stamp);
  while (stamps_.size() > window_) stamps_.pop_front();
}

double RateEstimator::rate(double now, double min_rate) const
{
  if (stamps_.size() < 2) return 0.0;
  const double span = stamps_.back() - stamps_.front();
  if (span <= 0.0) return 0.0;
  if (now - stamps_.back() > 2.0 / std::max(min_rate, 1e-3)) return 0.0;
  return static_cast<double>(stamps_.size() - 1) / span;
}

}  // namespace guide_robot_supervisor
