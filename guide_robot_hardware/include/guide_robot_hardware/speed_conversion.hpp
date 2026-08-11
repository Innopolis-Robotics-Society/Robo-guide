#ifndef GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_
#define GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_

namespace guide_robot_hardware {

/// Инверсия измеренной характеристики FURO:
///   actual_speed = speed_offset + speed_coefficient * motor_units.
/// Ненулевую скорость ниже offset привод воспроизвести не может.
constexpr double linearSpeedToMotorUnits(
  double linear_speed, double speed_coefficient, double speed_offset)
{
  if (linear_speed <= speed_offset) {
    return 0.0;
  }
  return (linear_speed - speed_offset) / speed_coefficient;
}

static_assert(linearSpeedToMotorUnits(0.0, 0.0001, 0.04) == 0.0);
static_assert(linearSpeedToMotorUnits(0.04, 0.0001, 0.04) == 0.0);
static_assert(linearSpeedToMotorUnits(0.14, 0.0001, 0.04) > 999.0);
static_assert(linearSpeedToMotorUnits(0.14, 0.0001, 0.04) < 1001.0);
static_assert(linearSpeedToMotorUnits(0.10, 0.0000880243, 0.0436513) > 639.0);
static_assert(linearSpeedToMotorUnits(0.10, 0.0000880243, 0.0436513) < 641.0);
static_assert(linearSpeedToMotorUnits(0.60, 0.0000880243, 0.0436513) > 6319.0);
static_assert(linearSpeedToMotorUnits(0.60, 0.0000880243, 0.0436513) < 6321.0);

}  // namespace guide_robot_hardware

#endif  // GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_
