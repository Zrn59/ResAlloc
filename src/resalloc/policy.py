"""Turn a provider snapshot into continue, migrate, park, or stop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from resalloc.models import Limits


class Action(Enum):
    CONTINUE = "continue"
    MIGRATE = "migrate"
    PARK = "park"
    STOP = "stop"


class Signal(Enum):
    """What the training script sees when it calls pulse()."""

    CONTINUE = "continue"
    CHECKPOINT_AND_MIGRATE = "checkpoint_and_migrate"
    STOP = "stop"


@dataclass(frozen=True)
class AccountSnapshot:
    provider: str
    alive: bool = True
    # Money still safe to spend. Floors and session caps are already applied.
    headroom_usd: float | None = None
    burn_usd_per_hour: float | None = None
    hourly_price_usd: float | None = None
    # Accelerator time still safe to use, after the configured minimum.
    quota_hours_left: float | None = None
    heartbeat_age_seconds: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    wait_for_ack: bool


def lease_bounds(
    snapshot: AccountSnapshot,
    limits: Limits,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Worker-side deadlines. A missing limit becomes a far-future lease."""

    remaining, _reason = time_to_limit(snapshot, limits)
    if remaining is None:
        horizon = now + timedelta(days=3650)
        return horizon, horizon
    soft_at = now + max(timedelta(0), remaining - limits.soft_buffer)
    hard_at = now + max(timedelta(0), remaining - limits.hard_buffer)
    return soft_at, hard_at


def signal_for(action: Action) -> Signal:
    if action is Action.STOP:
        return Signal.STOP
    return Signal.CHECKPOINT_AND_MIGRATE


def time_to_limit(snapshot: AccountSnapshot, limits: Limits) -> tuple[timedelta | None, str]:
    """How long the workload can keep running. None means no known limit."""

    if snapshot.detail == "completed":
        return timedelta(0), "completed"
    if not snapshot.alive:
        return timedelta(0), snapshot.detail or "workload_stopped"
    if (
        snapshot.provider == "laptop"
        and snapshot.heartbeat_age_seconds is not None
        and snapshot.heartbeat_age_seconds > limits.laptop_heartbeat_timeout.total_seconds()
    ):
        return timedelta(0), "laptop_heartbeat_lost"
    hourly_caps = {
        "runpod": (limits.runpod_max_hourly_usd, "runpod_hourly_price"),
        "vast": (limits.vast_max_hourly_usd, "vast_hourly_price"),
    }
    cap, cap_reason = hourly_caps.get(snapshot.provider, (None, ""))
    if cap is not None and snapshot.hourly_price_usd is not None and snapshot.hourly_price_usd > cap:
        return timedelta(0), cap_reason

    times: list[tuple[timedelta, str]] = []
    if snapshot.headroom_usd is not None:
        if snapshot.headroom_usd <= 0:
            times.append((timedelta(0), "budget_headroom"))
        elif snapshot.burn_usd_per_hour and snapshot.burn_usd_per_hour > 0:
            hours = snapshot.headroom_usd / snapshot.burn_usd_per_hour
            times.append((timedelta(seconds=hours * 3600), "budget_headroom"))
    if snapshot.quota_hours_left is not None:
        seconds = max(0.0, snapshot.quota_hours_left) * 3600
        times.append((timedelta(seconds=seconds), "accelerator_quota"))
    if not times:
        return None, ""
    return min(times, key=lambda item: item[0])


def decide(
    snapshot: AccountSnapshot,
    limits: Limits,
    *,
    has_next: bool,
    confirm_next: bool,
) -> Decision:
    remaining, reason = time_to_limit(snapshot, limits)
    if remaining is None:
        return Decision(Action.CONTINUE, "", True)
    # A script that exited non-zero is not an infrastructure failure. Moving it
    # spends the next provider on the same bug. Resume after the script is fixed.
    if reason in {"completed", "worker_failed"}:
        return Decision(Action.STOP, reason, False)
    if remaining <= limits.hard_buffer:
        wait = False
    elif remaining <= limits.soft_buffer:
        wait = True
    else:
        return Decision(Action.CONTINUE, reason, True)
    if not snapshot.alive or reason == "laptop_heartbeat_lost":
        wait = False
    if not has_next:
        action = Action.STOP
    elif confirm_next:
        action = Action.PARK
    else:
        action = Action.MIGRATE
    return Decision(action, reason, wait)
