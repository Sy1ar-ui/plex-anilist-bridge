"""The rate limiter, driven by a fake clock so no test actually waits."""
from __future__ import annotations

import asyncio

from app.ratelimit import WINDOW_SECONDS, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def build(limit: int = 3) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(limit, name="test", clock=clock, sleep=clock.sleep), clock


def test_requests_under_the_limit_do_not_wait():
    limiter, clock = build(3)
    asyncio.run(_acquire(limiter, 3))
    assert clock.slept == []


def test_the_limit_is_enforced_by_waiting_out_the_window():
    limiter, clock = build(3)
    asyncio.run(_acquire(limiter, 4))
    assert len(clock.slept) == 1
    assert clock.slept[0] == WINDOW_SECONDS


def test_the_window_slides():
    limiter, clock = build(2)
    asyncio.run(_acquire(limiter, 2))
    clock.now += WINDOW_SECONDS + 1
    asyncio.run(_acquire(limiter, 2))
    assert clock.slept == []


def test_the_limit_reported_by_the_server_is_adopted():
    """AniList is degraded to 30/min today; following the header beats guessing."""
    limiter, _ = build(90)
    limiter.observe({"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "29"})
    assert limiter.limit == 30
    assert limiter.remaining == 29


def test_an_exhausted_budget_blocks_until_the_window_passes():
    limiter, clock = build(90)
    limiter.observe({"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "0"})
    assert limiter.wait_time() == WINDOW_SECONDS
    clock.now += WINDOW_SECONDS
    assert limiter.wait_time() == 0


def test_retry_after_is_honoured_on_a_429():
    limiter, clock = build(30)
    delay = limiter.penalise({"Retry-After": "42"})
    assert delay == 42
    assert limiter.wait_time() == 42
    clock.now += 42
    assert limiter.wait_time() == 0


def test_rate_limit_reset_is_used_when_retry_after_is_absent(monkeypatch):
    limiter, _ = build(30)
    monkeypatch.setattr("time.time", lambda: 2_000_000.0)
    delay = limiter.penalise({"X-RateLimit-Reset": "2000030"})
    assert 29 <= delay <= 31


def test_a_429_with_no_useful_headers_still_backs_off():
    limiter, _ = build(30)
    assert limiter.penalise({}) == WINDOW_SECONDS


def test_a_silly_retry_after_is_clamped():
    limiter, _ = build(30)
    assert limiter.penalise({"Retry-After": "99999"}) == 300
    limiter2, _ = build(30)
    assert limiter2.penalise({"Retry-After": "0"}) == 1.0


async def _acquire(limiter: RateLimiter, times: int) -> None:
    for _ in range(times):
        await limiter.acquire()
