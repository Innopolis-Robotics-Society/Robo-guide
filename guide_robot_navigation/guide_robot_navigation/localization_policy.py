"""Deterministic, monotonic-time localization interlock policy (no ROS)."""

import math


class LocalizationPolicy:
    def __init__(
        self,
        *,
        latched=False,
        position_limit=0.5,
        yaw_limit=math.radians(20),
        recovery_position=0.35,
        recovery_yaw=math.radians(15),
    ):
        if (
            not all(
                math.isfinite(v) and v > 0
                for v in (position_limit, yaw_limit, recovery_position, recovery_yaw)
            )
            or recovery_position > position_limit
            or recovery_yaw > yaw_limit
        ):
            raise ValueError("Invalid localization thresholds")
        self.position_limit, self.yaw_limit = position_limit, yaw_limit
        self.recovery_position, self.recovery_yaw = recovery_position, recovery_yaw
        self.state = "lost" if latched else "initializing"
        self.reason = "operator_reset_required" if latched else "waiting_for_amcl"
        self.latched = latched
        self.last_sample = None
        self.position_sigma = self.yaw_sigma = None
        self.bad_since = self.good_since = self.reset_at = None
        self.good_count = 0
        self.costmaps_cleared = False

    def lose(self, reason):
        self.state, self.reason, self.latched = "lost", reason, True
        self.good_since, self.good_count = None, 0

    def reset(self, now):
        self.state, self.reason = "recovering", "checking_new_pose"
        self.reset_at, self.good_since, self.good_count = now, None, 0
        self.bad_since = self.last_sample = None
        self.costmaps_cleared = False

    def sample(self, covariance, now, *, tf_ok=True):
        try:
            xx, xy, yx, yy, yaw = (float(covariance[i]) for i in (0, 1, 6, 7, 35))
            if not all(math.isfinite(v) for v in (xx, xy, yx, yy, yaw)):
                raise ValueError()
            if min(xx, yy, yaw) < 0 or abs(xy - yx) > 1e-6 or xx * yy - xy * xy < -1e-9:
                raise ValueError()
            self.position_sigma = math.sqrt((xx + yy + math.hypot(xx - yy, 2 * xy)) / 2)
            self.yaw_sigma = math.sqrt(yaw)
            if not math.isfinite(self.position_sigma):
                self.position_sigma = None
                raise ValueError("invalid_covariance")
        except (ValueError, IndexError, TypeError):
            self.lose("invalid_covariance")
            return
        self.last_sample = now
        bad = self.position_sigma > self.position_limit or self.yaw_sigma > self.yaw_limit
        # Recovery already blocks motion. Allow the full convergence window;
        # ordinary one-second loss detection must not abort it prematurely.
        if bad and self.state != "recovering":
            if self.bad_since is None:
                self.bad_since = now
            if now - self.bad_since >= 1:
                self.lose("uncertainty_exceeded")
        else:
            self.bad_since = None
        if self.state == "initializing" and not bad and tf_ok:
            self.state, self.reason = "ready", ""
        if self.state == "recovering":
            good = (
                tf_ok
                and self.position_sigma <= self.recovery_position
                and self.yaw_sigma <= self.recovery_yaw
            )
            if good:
                if self.good_since is None:
                    self.good_since = now
                self.good_count += 1
            else:
                self.good_since, self.good_count = None, 0

    def tick(self, now, *, moving, tf_ok, amcl_active):
        if self.state == "ready":
            if not tf_ok or not amcl_active:
                self.lose("localization_unavailable")
            elif moving and (self.last_sample is None or now - self.last_sample > 3):
                self.lose("amcl_stale")
        if self.state == "recovering":
            if now - self.reset_at >= 15:
                self.lose("recovery_timeout")
            elif (
                self.costmaps_cleared
                and tf_ok
                and amcl_active
                and self.good_count >= 3
                and self.good_since is not None
                and now - self.good_since >= 2
                and self.last_sample is not None
                and now - self.last_sample <= 1
            ):
                self.state, self.reason, self.latched = "ready", "", False

    def snapshot(self, now):
        age = None if self.last_sample is None else max(0, now - self.last_sample)
        return dict(
            state=self.state,
            reason=self.reason,
            position_sigma=self.position_sigma,
            yaw_sigma=self.yaw_sigma,
            position_limit=self.position_limit,
            yaw_limit=self.yaw_limit,
            age_s=age,
            fresh=age is not None and age <= 3,
            motion_blocked=self.state != "ready",
            reset_required=self.latched,
        )
