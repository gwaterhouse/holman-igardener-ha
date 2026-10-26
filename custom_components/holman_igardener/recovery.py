"""Timing helpers for BLE recovery.

Pure functions and constants. No Home Assistant imports, so tests can
exercise the policy without a running core or a radio.
"""
from __future__ import annotations

# Let the proxy and the controller settle after Home Assistant starts
# before the first connection. A cached advertisement must not skip this.
STARTUP_DELAY_S = 25.0
# Pause after a link we held drops, so the controller can start advertising
# before we connect again. The attempt itself also listens for an advert.
RECONNECT_SOON_S = 3.0
# After a failed attempt. One in-flight connect, then a long quiet period.
# Repeated failures must not speed up just because advertisements continue.
BACKOFF_STEPS_S = (30.0, 60.0, 120.0, 180.0)
UNREACHABLE_REPAIR_S = 15 * 60.0
GATT_OP_TIMEOUT_S = 20.0
DISCONNECT_TIMEOUT_S = 10.0
# Outer bound around bleak-retry-connector's own retries.
CONNECT_BUDGET_S = 90.0
WATCHDOG_INTERVAL_S = 60.0
# Advertisements already in flight when the link comes up are not proof
# that the controller thinks it is disconnected.
STALE_ADV_GRACE_S = 15.0
CONNECT_ATTEMPTS = 3


class AttemptDeferred(Exception):
    """Connect was not tried. Do not count this as a failure."""


def backoff_seconds(failure_count: int) -> float:
    """Seconds to wait after failure_count consecutive failed attempts.

    failure_count is 1 for the first failure. Further failures stay at
    the last step.
    """
    if failure_count <= 0:
        return 0.0
    index = min(failure_count, len(BACKOFF_STEPS_S)) - 1
    return BACKOFF_STEPS_S[index]


def ready_in(now: float, startup_not_before: float, fail_until: float) -> float:
    """Seconds until a connection attempt is allowed."""
    return max(0.0, startup_not_before - now, fail_until - now)


def repair_due(
    now: float,
    wanted_since: float | None,
    connected: bool,
    *,
    after: float = UNREACHABLE_REPAIR_S,
) -> bool:
    """True when the link we want has been down long enough to ask for a power-cycle."""
    if connected or wanted_since is None:
        return False
    return (now - wanted_since) >= after


def unreachable_issue_id(address: str) -> str:
    """Stable repair id for one controller. Colons are not used in issue ids."""
    return "unreachable_" + address.replace(":", "").lower()
