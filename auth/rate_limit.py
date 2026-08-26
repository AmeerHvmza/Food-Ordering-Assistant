"""Per-key rate limiting: an in-memory burst bucket and a durable daily quota.

Two limits, stored differently because they fail differently:

* **requests/minute** — a token bucket in process memory. It exists to absorb
  bursts, and losing it on restart is harmless.
* **requests/day** — a counter in SQLite. A daily quota that resets whenever
  the process restarts is not a quota, so this one has to survive.

Known limitation, documented rather than quietly wrong: the minute bucket is
per process, so `uvicorn --workers 4` multiplies the effective burst limit by
four. The daily quota is unaffected because it lives in SQLite. This is the
specific reason to move the bucket to Redis later, which is why the limiter is
behind a protocol.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException, Request

from auth import store
from auth.api_keys import Principal

# Sweep idle buckets once the table gets big, so a long-lived process with many
# keys does not grow without bound.
_SWEEP_THRESHOLD = 1024
_IDLE_SECONDS = 300


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    scope: str  # "minute" or "day"

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset_after),
            "X-RateLimit-Scope": self.scope,
        }


class RateLimiter(Protocol):
    """Swap-in point for a Redis implementation. Nothing else needs to change."""

    def check(self, key_id: int, limit: int, burst: int) -> Decision: ...


class TokenBucket:
    """Classic token bucket: `limit` tokens refilled per minute, capped at `burst`."""

    def __init__(self) -> None:
        self._buckets: dict[int, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key_id: int, limit: int, burst: int) -> Decision:
        rate = limit / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key_id, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate)

            if tokens >= 1.0:
                self._buckets[key_id] = (tokens - 1.0, now)
                allowed, remaining = True, int(tokens - 1.0)
                retry = 0
            else:
                self._buckets[key_id] = (tokens, now)
                allowed, remaining = False, 0
                # Time until one whole token exists again.
                retry = max(1, int((1.0 - tokens) / rate) + 1)

            if len(self._buckets) > _SWEEP_THRESHOLD:
                self._sweep(now)

        return Decision(allowed, limit, remaining, retry, "minute")

    def _sweep(self, now: float) -> None:
        """Caller holds the lock."""
        stale = [
            key
            for key, (_, last) in self._buckets.items()
            if now - last > _IDLE_SECONDS
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_minute_limiter = TokenBucket()


def _seconds_until_utc_midnight() -> int:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int((midnight - now).total_seconds()))


def check_daily_quota(principal: Principal) -> Decision:
    """Reserve one request against today's quota.

    Read-then-write inside an IMMEDIATE transaction so two concurrent requests
    cannot both slip past the last unit of quota. Rejected requests are not
    counted, which keeps `usage_daily.requests` a meaningful record of what the
    tenant was actually served (rejections still land in `usage_events`).
    """
    limit = principal.tier.requests_per_day
    day = store.utc_day()
    reset = _seconds_until_utc_midnight()

    with store.session() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT requests FROM usage_daily WHERE key_id = ? AND day = ?",
                (principal.key_id, day),
            ).fetchone()
            used = int(row["requests"]) if row else 0

            if used >= limit:
                conn.rollback()
                return Decision(False, limit, 0, reset, "day")

            if row:
                conn.execute(
                    "UPDATE usage_daily SET requests = requests + 1 "
                    "WHERE key_id = ? AND day = ?",
                    (principal.key_id, day),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO usage_daily (key_id, tenant_id, day, requests)
                    VALUES (?, ?, ?, 1)
                    """,
                    (principal.key_id, principal.tenant_id, day),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return Decision(True, limit, limit - used - 1, reset, "day")


def enforce(request: Request, principal: Principal) -> dict[str, str]:
    """Apply both limits. Raises 429 with Retry-After, or returns headers.

    The burst limit is checked first so that a client hammering the endpoint
    does not burn daily quota it never got served.
    """
    minute = _minute_limiter.check(
        principal.key_id,
        principal.tier.requests_per_minute,
        principal.tier.burst,
    )
    if not minute.allowed:
        _reject(request, minute, principal, "per-minute")

    daily = check_daily_quota(principal)
    if not daily.allowed:
        _reject(request, daily, principal, "daily")

    headers = daily.headers()
    headers["X-RateLimit-Limit-Minute"] = str(minute.limit)
    headers["X-RateLimit-Remaining-Minute"] = str(minute.remaining)
    request.state.rate_headers = headers
    return headers


def _reject(
    request: Request, decision: Decision, principal: Principal, label: str
) -> None:
    headers = decision.headers()
    headers["Retry-After"] = str(decision.reset_after)
    request.state.rate_headers = headers
    raise HTTPException(
        status_code=429,
        detail=(
            f"{label} rate limit exceeded for tier '{principal.tier.name}' "
            f"({decision.limit} requests per {decision.scope}). "
            f"Retry in {decision.reset_after}s."
        ),
        headers=headers,
    )


def reset_for_tests() -> None:
    _minute_limiter.reset()
