#ifndef GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_
#define GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_

namespace guide_robot_hardware {

/// Инверсия измеренной характеристики FURO, кусочная:
///   выше сшивки — аффинная  v = speed_offset + speed_coefficient * units
///   (откалибрована автопрогонами 0.35-0.50 м/с),
///   ниже сшивки — пропорциональная  v = low_speed_coefficient * units
///   (коэффициент до 2026-08-11; малые команды ползут, как тогда).
///
/// Жёсткая мёртвая зона «ниже offset — стоп» сломала финальный доворот у цели:
/// DWB из покоя сэмплирует угловые скорости в окне ±acc_lim_theta/15 Гц ≈
/// ±0.13 рад/с, что ниже порога вращения offset/(wheel_separation/2) ≈
/// 0.25 рад/с — робот не мог НАЧАТЬ поворот.
///
/// Сшивка непрерывна: прямые v/k_low и (v-offset)/k пересекаются в
/// v_seam = offset * k_low / (k_low - k). Требование k_low > k > 0
/// проверяется в loadParameters().
constexpr double linearSpeedToMotorUnits(
  double linear_speed, double speed_coefficient, double speed_offset, double low_speed_coefficient)
{
  const double seam =
    speed_offset * low_speed_coefficient / (low_speed_coefficient - speed_coefficient);
  if (linear_speed >= seam) {
    return (linear_speed - speed_offset) / speed_coefficient;
  }
  return linear_speed / low_speed_coefficient;
}

static_assert(linearSpeedToMotorUnits(0.0, 0.0001, 0.04, 0.0002) == 0.0);
// seam = 0.04 * 0.0002 / 0.0001 = 0.08: ниже неё — старая прямая v / k_low.
static_assert(linearSpeedToMotorUnits(0.04, 0.0001, 0.04, 0.0002) > 199.0);
static_assert(linearSpeedToMotorUnits(0.04, 0.0001, 0.04, 0.0002) < 201.0);
static_assert(linearSpeedToMotorUnits(0.14, 0.0001, 0.04, 0.0002) > 999.0);
static_assert(linearSpeedToMotorUnits(0.14, 0.0001, 0.04, 0.0002) < 1001.0);
// seam = 0.0902 м/с: команда доворота 0.1 рад/с на колесе 0.0174 м/с ->
// ~102 units, как до введения аффинной конверсии.
static_assert(linearSpeedToMotorUnits(0.0174, 0.0000880243, 0.0436513, 0.0001706) > 101.0);
static_assert(linearSpeedToMotorUnits(0.0174, 0.0000880243, 0.0436513, 0.0001706) < 103.0);
static_assert(linearSpeedToMotorUnits(0.10, 0.0000880243, 0.0436513, 0.0001706) > 639.0);
static_assert(linearSpeedToMotorUnits(0.10, 0.0000880243, 0.0436513, 0.0001706) < 641.0);
static_assert(linearSpeedToMotorUnits(0.60, 0.0000880243, 0.0436513, 0.0001706) > 6319.0);
static_assert(linearSpeedToMotorUnits(0.60, 0.0000880243, 0.0436513, 0.0001706) < 6321.0);

}  // namespace guide_robot_hardware

#endif  // GUIDE_ROBOT_HARDWARE__SPEED_CONVERSION_HPP_
