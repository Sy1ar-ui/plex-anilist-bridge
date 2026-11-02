"""A pacer for APIs that publish their limits in response headers.

AniList is currently in a degraded state capped at 30 requests/minute (90
normally) and returns ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining``, with
``Retry-After`` and ``X-RateLimit-Reset`` on a 429. This class paces requests
under the limit it observes, and once a 429 arrives it refuses to send anything
until the reset passes.

The clock and sleep function are injectable so the behaviour can be tested
without real time passing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


class RateLimitWaitTooLong(Exception):
    """The next free slot is further away than the caller is willing to wait."""

    def __init__(self, wait_seconds: float, max_wait: float) -> None:
        super().__init__(
            f"next request slot is {wait_seconds:.0f}s away, "
            f"more than the {max_wait:.0f}s this caller will wait"
        )
        self.wait_seconds = wait_seconds
        self.max_wait = max_wait


class RateLimiter:
    def __init__(
        self,
        limit_per_minute: int = 30,
        *,
        name: str = "api",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self._limit = max(1, limit_per_minute)
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._blocked_until: float | None = None
        self._lock = asyncio.Lock()
        self.remaining: int | None = None

    @property
    def limit(self) -> int:
        return self._limit

    def _prune(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
            self._sent.popleft()

    def wait_time(self) -> float:
        """Seconds to wait before the next request may be sent."""
        now = self._clock()
        if self._blocked_until is not None:
            if now < self._blocked_until:
                return self._blocked_until - now
            self._blocked_until = None
        self._prune(now)
        if len(self._sent) < self._limit:
            return 0.0
        return max(0.0, WINDOW_SECONDS - (now - self._sent[0]))

    async def acquire(self, *, max_wait: float | None = None) -> None:
        """Block until a request may be sent, then record it.

        With ``max_wait``, raise :class:`RateLimitWaitTooLong` instead of
        sleeping longer than that. This is what lets a long AniList throttle
        defer an event instead of pinning a task (and the shared lock) for
        minutes.
        """
        if max_wait is not None:
            ahead = self.wait_time()
            if ahead > max_wait:
                raise RateLimitWaitTooLong(ahead, max_wait)
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=max(max_wait, 0.001))
            except TimeoutError:
                raise RateLimitWaitTooLong(max_wait, max_wait) from None
        else:
            await self._lock.acquire()
        try:
            while True:
                delay = self.wait_time()
                if delay <= 0:
                    break
                if max_wait is not None and delay > max_wait:
                    raise RateLimitWaitTooLong(delay, max_wait)
                log.debug("rate limit pause", extra={"api": self.name, "seconds": round(delay, 2)})
                await self._sleep(delay)
            self._sent.append(self._clock())
        finally:
            self._lock.release()

    def observe(self, headers) -> None:
        """Adopt the limit the server reports, so we follow it rather than guess."""
        limit = _header_int(headers, "X-RateLimit-Limit")
        if limit and limit != self._limit:
            log.info(
                "rate limit adjusted from response headers",
                extra={"api": self.name, "old": self._limit, "new": limit},
            )
            self._limit = max(1, limit)
        remaining = _header_int(headers, "X-RateLimit-Remaining")
        if remaining is not None:
            self.remaining = remaining
            # Treat an exhausted budget as a full window, since AniList starts a
            # cool-down as soon as remaining hits zero.
            if remaining <= 0:
                self._blocked_until = self._clock() + WINDOW_SECONDS

    def penalise(self, headers) -> float:
        """Record a 429 and return how long until requests may resume."""
        retry_after = _header_float(headers, "Retry-After")
        if retry_after is None:
            reset = _header_float(headers, "X-RateLimit-Reset")
            # X-RateLimit-Reset is a wall-clock unix timestamp, not a duration.
            retry_after = max(0.0, reset - time.time()) if reset else WINDOW_SECONDS
        retry_after = max(1.0, min(retry_after, 300.0))
        self._blocked_until = self._clock() + retry_after
        log.warning(
            "rate limited", extra={"api": self.name, "retry_after": round(retry_after, 2)}
        )
        return retry_after


def _header_int(headers, name: str) -> int | None:
    value = _header_float(headers, name)
    return int(value) if value is not None else None


def _header_float(headers, name: str) -> float | None:
    try:
        raw = headers.get(name)
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None
