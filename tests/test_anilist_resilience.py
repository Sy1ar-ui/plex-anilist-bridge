"""AniList being down, throttling hard, or serving error pages.

The client is driven through httpx's MockTransport and fake clocks, so nothing
touches the network and no test actually waits.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import tokens
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListThrottled,
    AniListUnavailable,
)
from app.anilist.health import (
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_OK,
    STATE_THROTTLED,
    AniListHealth,
)
from app.ratelimit import RateLimiter, RateLimitWaitTooLong


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


VIEWER_OK = {"data": {"Viewer": {"id": 1, "name": "me"}}}


def respond(status: int = 200, *, json=None, text=None, headers=None):
    if json is not None:
        return httpx.Response(status, json=json, headers=headers)
    return httpx.Response(status, text=text or "", headers=headers)


class Harness:
    """A client wired to a scripted transport, a fake clock and its own health."""

    def __init__(self, responses, *, max_wait=120.0, threshold=3, **client_kw):
        self.clock = FakeClock()
        self.health = AniListHealth(clock=self.clock, failure_threshold=threshold)
        self.pacer = RateLimiter(1000, clock=self.clock, sleep=self.clock.sleep)
        self.responses = list(responses)
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
            if isinstance(item, Exception):
                raise item
            return item

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = AniListClient(
            "token",
            http=self.http,
            pacer=self.pacer,
            health_tracker=self.health,
            max_wait=max_wait,
            sleep=self.clock.sleep,
            **client_kw,
        )

    def viewer(self):
        return asyncio.run(self.client.viewer())


# --------------------------------------------------------------------------
# the circuit breaker on its own
# --------------------------------------------------------------------------
def test_the_circuit_opens_after_repeated_failures():
    clock = FakeClock()
    health = AniListHealth(clock=clock, failure_threshold=3, base_cooldown=30)
    health.record_failure("HTTP 503")
    health.record_failure("HTTP 503")
    assert health.blocked_reason() is None
    assert health.state == STATE_DEGRADED

    health.record_failure("HTTP 503")
    assert health.state == STATE_DOWN
    assert health.circuit_open()
    assert "HTTP 503" in health.blocked_reason()
    assert health.retry_at() == clock.now + 30


def test_after_the_cooldown_one_request_is_let_through_and_success_closes_it():
    clock = FakeClock()
    health = AniListHealth(clock=clock, failure_threshold=1, base_cooldown=30)
    health.record_failure("down")
    clock.now += 31
    assert health.blocked_reason() is None  # half-open: try again
    assert health.state == STATE_DOWN       # but not healthy until it works

    health.record_success()
    assert health.state == STATE_OK
    assert health.last_recovered_at == clock.now


def test_a_failure_while_half_open_reopens_for_longer():
    clock = FakeClock()
    health = AniListHealth(clock=clock, failure_threshold=1, base_cooldown=30, max_cooldown=100)
    health.record_failure("down")
    assert health.open_until == clock.now + 30
    clock.now += 31
    health.record_failure("still down")
    assert health.open_until == clock.now + 60
    clock.now += 61
    health.record_failure("still down")
    assert health.open_until == clock.now + 100  # capped


def test_throttling_is_reported_but_does_not_trip_the_circuit():
    clock = FakeClock()
    health = AniListHealth(clock=clock)
    health.record_throttle(60)
    assert health.state == STATE_THROTTLED
    assert not health.circuit_open()
    assert health.blocked_reason() is None
    clock.now += 61
    assert health.state == STATE_OK


def test_the_snapshot_says_when_the_limit_is_below_normal():
    snap = AniListHealth().snapshot(limit=30, remaining=12)
    assert snap["limit_reduced"] is True
    assert snap["normal_limit_per_minute"] == 90
    assert AniListHealth().snapshot(limit=90)["limit_reduced"] is False


# --------------------------------------------------------------------------
# the rate limiter's patience
# --------------------------------------------------------------------------
def test_acquire_refuses_to_wait_longer_than_asked():
    clock = FakeClock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    limiter.penalise({"Retry-After": "200"})
    with pytest.raises(RateLimitWaitTooLong):
        asyncio.run(limiter.acquire(max_wait=60))
    assert clock.slept == []


def test_acquire_waits_when_the_wait_is_acceptable():
    clock = FakeClock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    limiter.penalise({"Retry-After": "20"})
    asyncio.run(limiter.acquire(max_wait=60))
    assert clock.slept == [20]


# --------------------------------------------------------------------------
# the client: outages
# --------------------------------------------------------------------------
def test_repeated_5xx_raises_unavailable_and_stops_once_the_circuit_opens(temp_db):
    h = Harness([respond(503, text="Service Unavailable")])
    with pytest.raises(AniListUnavailable) as raised:
        h.viewer()
    assert h.calls == 3  # threshold reached; the fourth attempt is not wasted
    assert h.health.state == STATE_DOWN
    assert raised.value.retry_at is not None


def test_while_the_circuit_is_open_nothing_is_sent(temp_db):
    h = Harness([respond(503)])
    with pytest.raises(AniListUnavailable):
        h.viewer()
    sent = h.calls
    with pytest.raises(AniListUnavailable, match="looks to be down"):
        h.viewer()
    assert h.calls == sent


def test_an_explicit_check_goes_through_an_open_circuit_and_closes_it(temp_db):
    h = Harness([respond(503)])
    with pytest.raises(AniListUnavailable):
        h.viewer()
    h.responses = [respond(json=VIEWER_OK)]
    h.client._respect_circuit = False
    assert h.viewer()["name"] == "me"
    assert h.health.state == STATE_OK


def test_a_single_blip_is_retried_and_succeeds(temp_db):
    h = Harness([respond(502), respond(json=VIEWER_OK)])
    assert h.viewer()["name"] == "me"
    assert h.calls == 2
    assert h.health.state == STATE_OK


def test_a_transport_error_counts_as_an_outage(temp_db):
    h = Harness([httpx.ConnectError("connection refused")])
    with pytest.raises(AniListUnavailable, match="could not reach AniList"):
        h.viewer()
    assert h.health.state == STATE_DOWN


def test_an_html_error_page_with_200_is_an_outage_not_a_crash(temp_db):
    h = Harness([respond(200, text="<html>Bad gateway</html>")])
    with pytest.raises(AniListUnavailable, match="non-JSON"):
        h.viewer()


def test_a_403_maintenance_page_does_not_mark_the_token_broken(temp_db):
    tokens.save(tokens.ANILIST, access_token="token")
    h = Harness([respond(403, text="<html>Cloudflare: checking your browser</html>")])
    with pytest.raises(AniListUnavailable):
        h.viewer()
    assert tokens.get(tokens.ANILIST).status == tokens.STATUS_OK


def test_a_403_api_disabled_message_is_an_outage(temp_db):
    tokens.save(tokens.ANILIST, access_token="token")
    body = {"data": None, "errors": [{"message": "API temporarily disabled", "status": 403}]}
    h = Harness([respond(403, json=body)])
    with pytest.raises(AniListUnavailable):
        h.viewer()
    assert tokens.get(tokens.ANILIST).status == tokens.STATUS_OK


def test_a_graphql_500_inside_a_200_is_an_outage(temp_db):
    body = {"data": None, "errors": [{"message": "Internal Server Error", "status": 500}]}
    h = Harness([respond(200, json=body), respond(json=VIEWER_OK)])
    assert h.viewer()["name"] == "me"
    assert h.calls == 2


# --------------------------------------------------------------------------
# the client: throttling
# --------------------------------------------------------------------------
def test_a_long_retry_after_defers_instead_of_sleeping(temp_db):
    h = Harness([respond(429, headers={"Retry-After": "600"})], max_wait=120)
    with pytest.raises(AniListThrottled):
        h.viewer()
    assert h.calls == 1
    assert h.clock.slept == []
    assert h.health.state == STATE_THROTTLED
    assert h.health.state != STATE_DOWN


def test_a_short_retry_after_is_waited_out(temp_db):
    h = Harness(
        [respond(429, headers={"Retry-After": "5"}), respond(json=VIEWER_OK)], max_wait=120
    )
    assert h.viewer()["name"] == "me"
    assert h.clock.slept == [5]


def test_a_queue_longer_than_max_wait_is_refused_before_sending(temp_db):
    h = Harness([respond(json=VIEWER_OK)], max_wait=10)
    h.pacer.penalise({"Retry-After": "90"})
    with pytest.raises(AniListThrottled):
        h.viewer()
    assert h.calls == 0


# --------------------------------------------------------------------------
# the client: auth is not an outage
# --------------------------------------------------------------------------
def test_a_401_marks_the_token_broken(temp_db):
    tokens.save(tokens.ANILIST, access_token="token")
    h = Harness([respond(401, json={"errors": [{"message": "Unauthorized"}]})])
    with pytest.raises(AniListAuthError):
        h.viewer()
    assert tokens.get(tokens.ANILIST).status == tokens.STATUS_BROKEN
    assert h.health.state == STATE_OK  # a bad token says nothing about AniList


def test_an_invalid_token_graphql_error_is_an_auth_failure(temp_db):
    tokens.save(tokens.ANILIST, access_token="token")
    body = {"data": None, "errors": [{"message": "Invalid token", "status": 400}]}
    h = Harness([respond(400, json=body)])
    with pytest.raises(AniListAuthError):
        h.viewer()
    assert tokens.get(tokens.ANILIST).status == tokens.STATUS_BROKEN


def test_trying_a_new_token_does_not_break_the_stored_one(temp_db):
    tokens.save(tokens.ANILIST, access_token="the-good-one")
    h = Harness([respond(401)], record_auth_failures=False)
    with pytest.raises(AniListAuthError):
        h.viewer()
    assert tokens.get(tokens.ANILIST).status == tokens.STATUS_OK


def test_not_found_is_an_answer_and_means_anilist_is_up(temp_db):
    body = {"data": None, "errors": [{"message": "Not Found.", "status": 404}]}
    h = Harness([respond(404, json=body)])
    assert asyncio.run(h.client._post("query", {})) == {}
    assert h.health.last_success_at is not None


def test_an_interactive_check_makes_one_attempt_and_does_not_sleep(temp_db):
    h = Harness([respond(503)], max_attempts=1, respect_circuit=False)
    with pytest.raises(AniListUnavailable):
        h.viewer()
    assert h.calls == 1
    assert h.clock.slept == []
