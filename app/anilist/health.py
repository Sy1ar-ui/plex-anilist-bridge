"""How AniList is doing right now: a circuit breaker plus the numbers the UI shows.

AniList has had long outages and has spent long stretches throttled well below
its normal limit. Without this, every webhook during an outage would spend
its retries sleeping against a dead endpoint and then be marked as failed for
good. With it:

* after a few consecutive failures the circuit **opens**, and requests fail
  fast with :class:`~app.anilist.client.AniListUnavailable` instead of
  hitting the network, so the event is *deferred* rather than lost;
* the circuit stays open for a cooldown that doubles each time it re-opens
  (30s, 60s, 2m ... capped at 15m), so a long outage is not hammered;
* once the cooldown passes, requests are allowed through again ("half-open").
  One success closes the circuit; one failure re-opens it for longer.

It is process-wide state, deliberately in memory: after a restart the first
request rediscovers reality, which costs one request.

The clock is injectable so the behaviour can be tested without waiting.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# AniList's documented normal limit. It has run degraded at 30/min for long
# periods; the limiter follows the real number from the headers, and the UI
# compares against this to say so.
NORMAL_LIMIT_PER_MINUTE = 90

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_THROTTLED = "throttled"
STATE_DOWN = "down"

FAILURE_THRESHOLD = 3
BASE_COOLDOWN_SECONDS = 30.0
MAX_COOLDOWN_SECONDS = 900.0
THROTTLE_WINDOW_SECONDS = 600.0


class AniListHealth:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        failure_threshold: int = FAILURE_THRESHOLD,
        base_cooldown: float = BASE_COOLDOWN_SECONDS,
        max_cooldown: float = MAX_COOLDOWN_SECONDS,
    ) -> None:
        self._clock = clock
        self.failure_threshold = max(1, failure_threshold)
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.reset()

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.times_opened = 0
        self.open_until: float | None = None
        self.down_since: float | None = None
        self.throttled_until: float | None = None
        self.last_error: str | None = None
        self.last_failure_at: float | None = None
        self.last_success_at: float | None = None
        self.last_recovered_at: float | None = None
        self._throttles: deque[float] = deque()

    # -- recording -----------------------------------------------------------
    def record_success(self) -> None:
        now = self._clock()
        if self.down_since is not None and self.consecutive_failures >= self.failure_threshold:
            log.info(
                "anilist recovered",
                extra={"outage_seconds": round(now - self.down_since, 1)},
            )
            self.last_recovered_at = now
        self.consecutive_failures = 0
        self.times_opened = 0
        self.open_until = None
        self.down_since = None
        self.last_success_at = now

    def record_failure(self, reason: str) -> None:
        """A request failed for a reason that says AniList itself is unwell."""
        now = self._clock()
        self.consecutive_failures += 1
        self.last_error = reason
        self.last_failure_at = now
        if self.down_since is None:
            self.down_since = now
        if self.consecutive_failures >= self.failure_threshold:
            cooldown = min(self.base_cooldown * (2 ** self.times_opened), self.max_cooldown)
            self.times_opened += 1
            self.open_until = now + cooldown
            log.warning(
                "anilist circuit open",
                extra={
                    "consecutive_failures": self.consecutive_failures,
                    "cooldown_seconds": cooldown,
                    "error": reason,
                },
            )

    def record_throttle(self, retry_after: float) -> None:
        """AniList answered 429. Throttling is not an outage, so it does not trip the circuit."""
        now = self._clock()
        self._throttles.append(now)
        self._prune(now)
        self.throttled_until = max(self.throttled_until or 0.0, now + retry_after)
        self.last_error = f"rate limited by AniList (HTTP 429), retry after {retry_after:.0f}s"

    # -- reading -------------------------------------------------------------
    def _prune(self, now: float) -> None:
        while self._throttles and now - self._throttles[0] > THROTTLE_WINDOW_SECONDS:
            self._throttles.popleft()

    def circuit_open(self) -> bool:
        return self.open_until is not None and self._clock() < self.open_until

    def is_throttled(self) -> bool:
        return self.throttled_until is not None and self._clock() < self.throttled_until

    def recent_throttles(self) -> int:
        self._prune(self._clock())
        return len(self._throttles)

    def retry_at(self) -> float | None:
        """When it is next worth trying, or None if it is worth trying now."""
        candidates = []
        if self.circuit_open():
            candidates.append(self.open_until)
        if self.is_throttled():
            candidates.append(self.throttled_until)
        return max(candidates) if candidates else None

    def blocked_reason(self) -> str | None:
        """Why a request should not be sent at all right now, if it should not."""
        if not self.circuit_open():
            return None
        assert self.open_until is not None
        wait = self.open_until - self._clock()
        return (
            f"AniList looks to be down ({self.last_error or 'repeated failures'}); "
            f"not trying again for {_duration(wait)}"
        )

    @property
    def state(self) -> str:
        if self.consecutive_failures >= self.failure_threshold:
            # Half-open still counts as down until something actually succeeds.
            return STATE_DOWN
        if self.is_throttled():
            return STATE_THROTTLED
        if self.consecutive_failures or self.recent_throttles() >= 3:
            return STATE_DEGRADED
        return STATE_OK

    def snapshot(self, *, limit: int | None = None, remaining: int | None = None) -> dict[str, Any]:
        """Everything the dashboard and /health need, as plain values."""
        state = self.state
        retry_at = self.retry_at()
        return {
            "state": state,
            "summary": self._summary(state),
            "circuit_open": self.circuit_open(),
            "retry_at": _iso(retry_at),
            "down_since": _iso(self.down_since) if state == STATE_DOWN else None,
            "last_error": self.last_error,
            "last_failure_at": _iso(self.last_failure_at),
            "last_success_at": _iso(self.last_success_at),
            "last_recovered_at": _iso(self.last_recovered_at),
            "consecutive_failures": self.consecutive_failures,
            "throttles_last_10_min": self.recent_throttles(),
            "limit_per_minute": limit,
            "normal_limit_per_minute": NORMAL_LIMIT_PER_MINUTE,
            "limit_reduced": bool(limit and limit < NORMAL_LIMIT_PER_MINUTE),
            "remaining": remaining,
        }

    def _summary(self, state: str) -> str:
        if state == STATE_DOWN:
            if self.circuit_open():
                return "AniList is not responding. Events are being deferred and will be retried automatically."
            return "AniList was not responding. The next request will test whether it is back."
        if state == STATE_THROTTLED:
            return "AniList is rate limiting this client. Requests resume when the cooldown ends."
        if state == STATE_DEGRADED:
            if self.consecutive_failures:
                return "A recent AniList request failed. Retrying as normal."
            return "AniList has rate limited this client several times in the last 10 minutes."
        if self.last_success_at is None:
            return "No AniList requests made since the bridge started."
        return "AniList is responding normally."


def _iso(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="seconds")


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h"


_health = AniListHealth()


def health() -> AniListHealth:
    return _health
