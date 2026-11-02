"""The management UI: every page renders, and the outage states are visible."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, repo, settings_store, tokens
from app.anilist.client import AniListUnavailable
from app.anilist.health import health
from app.main import create_app
from app.models import AniListEntryState
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from app.web import timefmt
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event


def make_event(**kw):
    """As conftest's, but received now: deferral gives up on events days old."""
    from dataclasses import replace

    return replace(_make_event(**kw), received_at=datetime.now(UTC))


@pytest.fixture
def ui(datasets, settings):
    # No `with`: the lifespan (scheduler, dataset download) is not started.
    return TestClient(create_app())


class DownAniList(FakeAniList):
    async def media_with_entry(self, anilist_id: int):
        raise AniListUnavailable("AniList returned HTTP 503")


def deferred_event() -> int:
    state = AniListEntryState(anilist_id=290, exists=True, status="CURRENT", total_episodes=13)
    result = asyncio.run(
        handle_watch_event(
            make_event(tvdb_id=72025, season=1, episode=1),
            anilist=DownAniList(state),
            gate=WriteGate(dry_run=True),
        )
    )
    assert result.status == "deferred"
    return result.event_id


def trip_circuit() -> None:
    for _ in range(health().failure_threshold):
        health().record_failure("AniList returned HTTP 503")


# --------------------------------------------------------------------------
# pages render
# --------------------------------------------------------------------------
def test_the_dashboard_renders_on_a_fresh_install(ui):
    page = ui.get("/")
    assert page.status_code == 200
    assert "Finish setting up" in page.text
    assert "Recent activity" in page.text
    # Until a webhook arrives, setup shows the URL to paste into Plex.
    assert "/webhook/plex/" in page.text
    # The machinery lives on the System page, not here.
    assert "AniList API" not in page.text


def test_the_system_page_holds_the_machinery(ui):
    page = ui.get("/system")
    assert page.status_code == 200
    for text in ("AniList API", "Mapping datasets", "Scheduled jobs", "/webhook/plex/"):
        assert text in page.text, text


def test_dry_run_is_shown_on_every_page(ui):
    for path in ("/", "/activity", "/system", "/settings"):
        assert "pill-dry" in ui.get(path).text, path


def test_settings_render_with_the_outage_options(ui):
    page = ui.get("/settings")
    assert page.status_code == 200
    assert 'id="outages"' in page.text
    assert 'name="anilist_retry_interval_minutes"' in page.text


def test_activity_and_event_pages_render(ui):
    event_id = deferred_event()
    assert ui.get("/activity").status_code == 200
    page = ui.get(f"/activity/{event_id}")
    assert page.status_code == 200
    assert "Retry now" in page.text


# --------------------------------------------------------------------------
# outage visibility
# --------------------------------------------------------------------------
def test_an_outage_is_announced_on_every_page(ui):
    deferred_event()
    trip_circuit()
    for path in ("/", "/activity", "/settings"):
        page = ui.get(path)
        assert "AniList is not responding" in page.text, path
        assert "1 deferred" in page.text, path


def test_throttling_is_announced(ui):
    health().record_throttle(120)
    page = ui.get("/")
    assert "rate limiting this client" in page.text


def test_a_rejected_token_is_announced_with_a_way_to_fix_it(ui):
    tokens.save(tokens.ANILIST, access_token="t")
    tokens.mark_broken(tokens.ANILIST, "AniList rejected the token (HTTP 401)")
    page = ui.get("/")
    assert "The AniList token was rejected" in page.text
    assert "/settings#anilist" in page.text


def test_the_deferred_filter_explains_itself(ui):
    deferred_event()
    page = ui.get("/activity?status=deferred")
    assert "nothing is lost" in page.text
    assert "Retry deferred now" in page.text


def test_activity_shows_counts_per_status(ui):
    deferred_event()
    page = ui.get("/activity")
    assert 'href="/activity?status=deferred"' in page.text


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def test_retry_all_refuses_politely_while_anilist_is_down(ui):
    tokens.save(tokens.ANILIST, access_token="t")
    deferred_event()
    trip_circuit()
    response = ui.post("/actions/retry-deferred", data={"back": "/"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Not+retrying+yet" in response.headers["location"]
    assert "level=error" in response.headers["location"]


def test_retry_all_will_not_redirect_off_site(ui):
    response = ui.post(
        "/actions/retry-deferred", data={"back": "//evil.example"}, follow_redirects=False
    )
    assert response.headers["location"].startswith("/?")


def test_behaviour_settings_save_and_clamp_the_outage_options(ui):
    response = ui.post(
        "/settings/behaviour",
        data={
            "dry_run": "on",
            "anilist_retry_interval_minutes": "0",
            "anilist_retry_give_up_days": "14",
            "anilist_max_wait_seconds": "100000",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert settings_store.get("anilist_retry_interval_minutes") == 1
    assert settings_store.get("anilist_retry_give_up_days") == 14
    assert settings_store.get("anilist_max_wait_seconds") == 600


def test_reconcile_without_a_scheduler_still_answers(ui):
    response = ui.post("/actions/reconcile", follow_redirects=False)
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]


def test_health_reports_anilist_without_failing_the_container(ui):
    deferred_event()
    trip_circuit()
    response = ui.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["anilist_api"]["state"] == "down"
    assert body["deferred"] == 1
    assert any("deferred" in note for note in body["notes"])


# --------------------------------------------------------------------------
# time formatting
# --------------------------------------------------------------------------
def test_relative_times_read_naturally():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert timefmt.relative(now - timedelta(seconds=10), now=now) == "just now"
    assert timefmt.relative(now - timedelta(minutes=5), now=now) == "5 min ago"
    assert timefmt.relative(now + timedelta(minutes=5), now=now) == "in 5 min"
    assert timefmt.relative(now - timedelta(hours=3), now=now) == "3 h ago"
    assert timefmt.relative(now - timedelta(days=3), now=now) == "3 days ago"
    assert timefmt.relative(None) == "never"


def test_both_stored_timestamp_shapes_are_understood():
    assert timefmt.parse("2026-11-02 12:00:00") == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert timefmt.parse("2026-11-02T12:00:00+00:00") == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert timefmt.parse("nonsense") is None
    assert "title=" in str(timefmt.time_tag("2026-11-02 12:00:00"))


def test_status_counts_cover_a_recent_window(datasets, settings):
    deferred_event()
    assert repo.status_counts(since_hours=24 * 365 * 10).get("deferred") == 1
    assert repo.status_counts().get("deferred") == 1
    db.execute("UPDATE event SET received_at = '2000-01-01T00:00:00+00:00'")
    assert repo.status_counts(since_hours=24) == {}


# --------------------------------------------------------------------------
# rating a finished series
# --------------------------------------------------------------------------
def finished_series() -> int:
    """Watch the finale of a 13-episode series; returns the pending id."""
    from app.sync import completion

    state = AniListEntryState(
        anilist_id=290, exists=True, status="CURRENT", progress=12,
        total_episodes=13, media_status="FINISHED", title="Crest of the Stars",
    )
    result = asyncio.run(
        handle_watch_event(
            make_event(
                tvdb_id=72025, season=1, episode=13,
                viewed_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
            ),
            anilist=FakeAniList(state),
            gate=WriteGate(dry_run=True),
        )
    )
    assert result.status == "awaiting_rating"
    [pending] = completion.list_open()
    return pending["id"]


def test_a_finished_series_waits_on_the_dashboard(ui):
    pending_id = finished_series()
    page = ui.get("/")
    assert "Ready to rate" in page.text
    assert "Crest of the Stars" in page.text
    assert "12/13" in page.text
    assert "finished 2 Nov 2026" in page.text
    assert f'action="/ratings/{pending_id}"' in page.text
    assert "1 to rate" in page.text


def test_the_event_page_points_at_the_rating(ui):
    finished_series()
    event = db.query_one("SELECT id FROM event WHERE status = 'awaiting_rating'")
    page = ui.get(f"/activity/{event['id']}")
    assert "until you rate it" in page.text
    assert "2 Nov 2026" in page.text


def test_a_rating_is_kept_while_anilist_is_not_connected(ui):
    from app.sync import completion

    pending_id = finished_series()
    response = ui.post(
        f"/ratings/{pending_id}", data={"action": "rate", "score": "8.5"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Rating+saved" in response.headers["location"]
    item = completion.get(pending_id)
    assert item["status"] == "queued"
    assert item["score"] == 8.5
    assert "waiting for AniList" in ui.get("/").text


def test_a_bad_rating_is_refused(ui):
    from app.sync import completion

    pending_id = finished_series()
    response = ui.post(
        f"/ratings/{pending_id}", data={"action": "rate", "score": "11"},
        follow_redirects=False,
    )
    assert "level=error" in response.headers["location"]
    assert completion.get(pending_id)["status"] == "awaiting"


def test_dont_complete_removes_it_from_the_dashboard(ui):
    pending_id = finished_series()
    ui.post(f"/ratings/{pending_id}", data={"action": "dismiss"}, follow_redirects=False)
    assert "Ready to rate" not in ui.get("/").text


def test_the_rating_setting_saves(ui):
    ui.post("/settings/behaviour", data={"dry_run": "on"}, follow_redirects=False)
    assert settings_store.get("rate_before_complete") is False
    ui.post(
        "/settings/behaviour", data={"dry_run": "on", "rate_before_complete": "on"},
        follow_redirects=False,
    )
    assert settings_store.get("rate_before_complete") is True


def test_calendar_dates_read_naturally():
    assert timefmt.day("2026-11-02") == "2 Nov 2026"
    assert timefmt.day(None) == ""
