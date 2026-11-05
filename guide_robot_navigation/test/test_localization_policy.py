"""Localization uncertainty, persistence and operator recovery policy."""

import math
import pytest
from guide_robot_navigation.localization_policy import LocalizationPolicy


def cov(x=0.01, y=0.01, yaw=0.01, xy=0):
    c = [0.0] * 36
    c[0], c[7], c[35], c[1], c[6] = x, y, yaw, xy, xy
    return c


def test_correlated_position_and_latched_loss():
    p = LocalizationPolicy(position_limit=0.5)
    p.sample(cov(), 0)
    assert p.state == "ready"
    p.sample(cov(0.2, 0.2, xy=0.1), 1)
    assert math.isclose(p.position_sigma, math.sqrt(0.3))
    assert p.state == "ready"
    p.sample(cov(0.2, 0.2, xy=0.1), 2)
    assert p.state == "lost"
    p.sample(cov(), 3)
    assert p.state == "lost"


def test_recovery_requires_samples_time_and_costmaps():
    p = LocalizationPolicy(latched=True)
    p.reset(10)
    for t in (10.1, 11.1, 12.2):
        p.sample(cov(), t)
    p.tick(12.2, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == "recovering"
    p.costmaps_cleared = True
    p.tick(12.3, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == "ready" and not p.latched


def test_stale_while_moving_and_recovery_timeout():
    p = LocalizationPolicy()
    p.sample(cov(), 0)
    p.tick(4, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == "ready"
    p.tick(4, moving=True, tf_ok=True, amcl_active=True)
    assert p.reason == "amcl_stale"
    p.reset(5)
    p.tick(20, moving=False, tf_ok=True, amcl_active=True)
    assert p.reason == "recovery_timeout"


def test_invalid_covariance_and_transient_outlier():
    p = LocalizationPolicy()
    p.sample(cov(), 0)
    p.sample(cov(yaw=1), 1)
    p.sample(cov(), 1.5)
    assert p.state == "ready"
    p.sample(cov(x=float("nan")), 2)
    assert p.reason == "invalid_covariance"


def test_recovery_cannot_use_an_old_good_sample():
    p = LocalizationPolicy(latched=True)
    p.sample(cov(), 0)
    p.reset(5)
    p.costmaps_cleared = True
    p.tick(8, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == "recovering"


def test_bad_tf_does_not_initialize_and_loss_does_not_self_clear():
    p = LocalizationPolicy()
    p.sample(cov(), 0, tf_ok=False)
    assert p.state == "initializing"
    p.sample(cov(), 1)
    p.tick(1, moving=False, tf_ok=False, amcl_active=True)
    p.sample(cov(), 2)
    assert p.state == "lost"


def test_recovery_can_converge_near_end_of_full_window():
    p = LocalizationPolicy(latched=True)
    p.reset(0)
    p.costmaps_cleared = True
    for t in (0.1, 1.2, 5, 11):
        p.sample(cov(x=1, yaw=1), t)
        p.tick(t, moving=False, tf_ok=True, amcl_active=True)
        assert p.state == 'recovering' and p.latched
    for t in (12, 13, 14.1):
        p.sample(cov(), t)
        p.tick(t, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == 'ready' and not p.latched


def test_bad_recovery_uses_full_deadline_and_stays_blocked():
    p = LocalizationPolicy(latched=True)
    p.reset(0)
    p.costmaps_cleared = True
    for t in (0, 2, 10, 14.99):
        p.sample(cov(x=1), t)
        p.tick(t, moving=False, tf_ok=True, amcl_active=True)
        assert p.state == 'recovering'
        assert p.snapshot(t)['motion_blocked']
    p.tick(15, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == 'lost' and p.reason == 'recovery_timeout' and p.latched


def test_observed_amcl_roundoff_is_accepted_including_recovery():
    c = cov(x=-5.171088219367342e-13, y=9.693705857610324e-13,
            xy=-5.781448210476588e-12, yaw=2.0025536784474055e-11)
    p = LocalizationPolicy()
    p.sample(c, 0)
    assert p.state == 'ready'
    assert 0 <= p.position_sigma < 0.00001
    p.lose('operator_reset_required')
    p.sample(c, 1)
    assert p.latched and p.state == 'lost'
    p.reset(2)
    p.costmaps_cleared = True
    for t in (2.1, 3.1, 4.2):
        p.sample(c, t)
    p.tick(4.2, moving=False, tf_ok=True, amcl_active=True)
    assert p.state == 'ready' and not p.latched


def test_tiny_negative_yaw_and_xy_eigenvalues_are_clamped():
    p = LocalizationPolicy()
    p.sample(cov(x=-1e-12, y=-1e-12, yaw=-1e-12), 0)
    assert p.state == 'ready'
    assert p.position_sigma == p.yaw_sigma == 0


@pytest.mark.parametrize('c', [
    cov(x=-1e-6), cov(yaw=-1e-6), cov(x=0, y=0, xy=1e-6),
    cov(x=1, y=1, xy=1.01), cov(x=float('inf')),
    cov(yaw=float('nan')), cov(x=1e308, y=1e308),
])
def test_materially_invalid_covariance_still_blocks(c):
    p = LocalizationPolicy()
    p.sample(c, 0)
    assert p.reason == 'invalid_covariance' and p.latched


def test_asymmetric_covariance_is_rejected():
    c = cov()
    c[1] = 1e-5
    p = LocalizationPolicy()
    p.sample(c, 0)
    assert p.reason == 'invalid_covariance'


def test_two_metre_default_threshold_retains_dwell_and_yaw_limit():
    p = LocalizationPolicy()
    for t in (0, 1, 2):
        p.sample(cov(x=4), t)
    assert p.state == 'ready' and p.position_sigma == 2
    p.sample(cov(x=4.01), 3)
    assert p.state == 'ready'
    p.sample(cov(x=4.01), 4)
    assert p.reason == 'uncertainty_exceeded'
    p = LocalizationPolicy()
    for t in (0, 1):
        p.sample(cov(yaw=math.radians(21)**2), t)
    assert p.reason == 'uncertainty_exceeded'
