#include <gtest/gtest.h>

#include "guide_robot_supervisor/rate_estimator.hpp"

using guide_robot_supervisor::RateEstimator;

TEST(RateEstimator, NeedsTwoSamples)
{
  RateEstimator est(10);
  EXPECT_EQ(est.rate(0.0, 1.0), 0.0);
  est.add(0.0);
  EXPECT_EQ(est.rate(0.0, 1.0), 0.0);
}

TEST(RateEstimator, SteadyRate)
{
  RateEstimator est(10);
  for (int i = 0; i <= 20; ++i) est.add(i * 0.1);
  EXPECT_NEAR(est.rate(2.0, 1.0), 10.0, 1e-9);
}

TEST(RateEstimator, WindowFollowsTheLatestSamples)
{
  RateEstimator est(5);
  for (int i = 0; i < 10; ++i) est.add(i * 1.0);             // 1 Гц
  for (int i = 0; i < 4; ++i) est.add(9.0 + (i + 1) * 0.1);  // затем 10 Гц
  // в окне 5 меток: 9.0 .. 9.4
  EXPECT_NEAR(est.rate(9.4, 1.0), 10.0, 1e-9);
}

TEST(RateEstimator, StaleBufferDecaysToZero)
{
  RateEstimator est(10);
  for (int i = 0; i <= 10; ++i) est.add(i * 0.1);
  // min_rate 5 Гц -> протухание после 2 / 5 = 0.4 с тишины
  EXPECT_GT(est.rate(1.0 + 0.39, 5.0), 0.0);
  EXPECT_EQ(est.rate(1.0 + 0.41, 5.0), 0.0);
}

TEST(RateEstimator, SameStampsGiveZero)
{
  RateEstimator est(10);
  est.add(1.0);
  est.add(1.0);
  EXPECT_EQ(est.rate(1.0, 1.0), 0.0);
}

TEST(RateEstimator, ClearForgetsEverything)
{
  RateEstimator est(10);
  for (int i = 0; i <= 10; ++i) est.add(i * 0.1);
  est.clear();
  EXPECT_EQ(est.rate(1.0, 1.0), 0.0);
}
