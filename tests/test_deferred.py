"""Deferral and catch-up: nothing is lost while AniList is unavailable."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import db, repo, settings_store, tokens
from app.anilist.client import AniListAuthError, AniListUnavailable
from app.anilist.health import health
from app.models import AniListEntryState, WatchEvent
from app.sync import retry
from app.sync.pipeline import handle_watch_event, retry_delay
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event


def make_event(**kw):
    """As conftest's, but received now: deferral gives up on events days old."""
    from dataclasses import replace

    return replace(_make_event(**kw), received_at=datetime.now(UTC))


def entry(**kw) -> AniListEntryState:
    defaults = {
        "anilist_id": 290,
        "exists": True,
        "status": "CURRENT",
        "progress": 0,
        "total_episodes": 13,
        "media_status": "FINISHED",
        "title": "Crest of the Stars",
    }
    defaults.update(kw)
    return AniListEntryState(**defaults)


class FlakyAniList(FakeAniList):
    """A FakeAniList that can be told to be down, and for which calls."""

    def __init__(self, *args, down_reads=False, down_writes=False, down_search=False,
                 error=None, trips_circuit=False, **kw):
        super().__init__(*args, **kw)
        self.down_reads = down_reads
        self.down_writes = down_writes
        self.down_search = down_search
        self.error = error or AniListUnavailable("AniList returned HTTP 503")
        self.trips_circuit = trips_circuit

    def _fail(self):
        if self.trips_circuit:
            for _ in range(health().failure_threshold):
                health().record_failure("HTTP 503")
        raise self.error

    async def media_with_entry(self, anilist_id: int):
        self.reads += 1
        if self.down_reads:
            self._fail()
        return self.state, self.media

    async def save_entry(self, variables: dict):
        if self.down_writes:
            self._fail()
        return await super().save_entry(variables)

    async def search(self, title: str, per_page: int = 10):
        if self.down_search:
            self._fail()
        return await super().search(title, per_page)


def media():
    return {"id": 290, "episodes": 13, "title": {"romaji": "Crest of the Stars"}}


def run(event, client, *, dry_run=True, event_id=None):
    return asyncio.run(
        handle_watch_event(
            event, anilist=client, gate=WriteGate(dry_run=dry_run), event_id=event_id
        )
    )


def event_row(event_id: int) -> dict:
    return dict(db.query_one("SELECT * FROM event WHERE id = ?", (event_id,)))


# --------------------------------------------------------------------------
# deferral in the pipeline
# --------------------------------------------------------------------------
def test_an_outage_defers_the_event_instead_of_failing_it(datasets, settings):
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client)

    assert result.status == "deferred"
    row = event_row(result.event_id)
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert row["anilist_id"] == 290  # the match is kept, only the AniList step waits
    assert "HTTP 503" in row["detail"]
    assert repo.deferred_count() == 1


def test_a_deferred_write_is_logged_as_deferred_not_as_a_failure(datasets, settings):
    client = FlakyAniList(entry(), media=media(), down_writes=True)
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client, dry_run=False)

    assert result.status == "deferred"
    rows = [dict(r) for r in db.query("SELECT result, error FROM write_log")]
    assert rows == [{"result": "deferred", "error": "AniList returned HTTP 503"}]


def test_an_outage_during_title_search_defers_rather_than_calling_it_unmatched(datasets, settings):
    client = FlakyAniList(entry(), down_search=True)
    result = run(make_event(tvdb_id=424242, season=1, episode=1), client)

    assert result.status == "deferred"
    assert repo.pending_review_count() == 0


def test_a_rejected_token_defers_until_reauthorised(datasets, settings):
    client = FlakyAniList(
        entry(), media=media(), down_reads=True, error=AniListAuthError("token rejected")
    )
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client)

    assert result.status == "deferred"
    assert "re-authorisation" in result.detail


def test_each_deferral_backs_off_further(datasets, settings):
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    event = make_event(tvdb_id=72025, season=1, episode=1)
    first = run(event, client)
    second = run(event, client, event_id=first.event_id)

    assert second.event_id == first.event_id
    assert event_row(first.event_id)["attempts"] == 2
    assert retry_delay(1) == timedelta(minutes=5)
    assert retry_delay(2) == timedelta(minutes=10)
    assert retry_delay(20) == timedelta(hours=1)


def test_the_circuits_retry_time_is_respected(datasets, settings):
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    later = (datetime.now(UTC) + timedelta(hours=3)).timestamp()
    client.error = AniListUnavailable("down", retry_at=later)
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client)

    due = datetime.fromisoformat(event_row(result.event_id)["next_attempt_at"]).replace(tzinfo=UTC)
    assert due >= datetime.fromtimestamp(later, UTC) - timedelta(seconds=1)


def test_an_old_event_is_given_up_on_and_says_so(datasets, settings):
    settings_store.set("anilist_retry_give_up_days", 2)
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    event = make_event(tvdb_id=72025, season=1, episode=1)
    from dataclasses import replace

    old = replace(event, received_at=datetime.now(UTC) - timedelta(days=3))
    result = run(old, client)

    assert result.status == "error"
    assert "gave up" in result.detail
    assert event_row(result.event_id)["next_attempt_at"] is None


def test_a_replayed_webhook_for_a_deferred_event_is_processed_again(datasets, settings):
    """Deferred is not terminal, so Plex re-sending the same play is useful."""
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    event = make_event(tvdb_id=72025, season=1, episode=1)
    first = run(event, client)

    client.down_reads = False
    second = run(event, client)

    assert second.status == "would_write"
    assert second.event_id == first.event_id
    assert event_row(first.event_id)["next_attempt_at"] is None
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 1


# --------------------------------------------------------------------------
# rebuilding the input
# --------------------------------------------------------------------------
def test_a_watch_event_round_trips_through_json():
    event = make_event(tvdb_id=72025, viewed_at=datetime(2026, 11, 1, 9, 30, tzinfo=UTC))
    assert WatchEvent.from_json(event.to_json()) == event


def test_rows_from_before_input_json_are_rebuilt_from_columns(datasets, settings):
    client = FlakyAniList(entry(), media=media(), down_reads=True)
    event = make_event(tvdb_id=72025, plex_guid="plex://show/abc", season=1, episode=3)
    result = run(event, client)
    db.execute("UPDATE event SET input_json = NULL WHERE id = ?", (result.event_id,))

    rebuilt = repo.event_input(result.event_id)
    assert rebuilt.show.tvdb_id == 72025
    assert (rebuilt.season, rebuilt.episode) == (1, 3)
    assert rebuilt.rating_key == event.rating_key


# --------------------------------------------------------------------------
# the retry job
# --------------------------------------------------------------------------
@pytest.fixture
def connected(datasets, settings, monkeypatch):
    """AniList connected, with AniListClient.from_db handing out a controllable fake."""
    tokens.save(tokens.ANILIST, access_token="token", account_name="me")
    client = FlakyAniList(entry(), media=media())

    class Factory:
        @staticmethod
        def from_db(**_kw):
            return client

    monkeypatch.setattr(retry, "AniListClient", Factory)
    monkeypatch.setattr(retry.plex_client.PlexClient, "from_db", classmethod(
        lambda cls: (_ for _ in ()).throw(retry.PlexUnavailable("no plex in tests"))
    ))
    return client


def defer_one(client, *, rating_key="1", episode=1) -> int:
    client.down_reads = True
    result = run(make_event(tvdb_id=72025, season=1, episode=episode, rating_key=rating_key), client)
    client.down_reads = False
    assert result.status == "deferred"
    return result.event_id


def test_the_retry_job_waits_for_each_events_backoff(connected):
    defer_one(connected)
    report = asyncio.run(retry.retry_deferred())
    assert report.attempted == 0
    assert "no deferred events are due" in report.summary()


def test_the_retry_job_catches_up_once_anilist_is_back(connected):
    event_id = defer_one(connected)
    db.execute("UPDATE event SET next_attempt_at = datetime('now', '-1 minute')")

    report = asyncio.run(retry.retry_deferred())

    assert report.recovered == 1
    assert event_row(event_id)["status"] == "would_write"
    assert repo.deferred_count() == 0


def test_force_ignores_backoff(connected):
    defer_one(connected)
    report = asyncio.run(retry.retry_deferred(force=True))
    assert report.recovered == 1


def test_the_retry_job_does_nothing_while_the_circuit_is_open(connected):
    event_id = defer_one(connected)
    for _ in range(health().failure_threshold):
        health().record_failure("HTTP 503")
    reads = connected.reads

    report = asyncio.run(retry.retry_deferred(force=True))

    assert report.attempted == 0
    assert "looks to be down" in report.summary()
    assert connected.reads == reads
    assert event_row(event_id)["status"] == "deferred"


def test_the_retry_job_does_nothing_while_the_token_is_broken(connected):
    defer_one(connected)
    tokens.mark_broken(tokens.ANILIST, "rejected")
    report = asyncio.run(retry.retry_deferred(force=True))
    assert report.attempted == 0
    assert "re-authorising" in report.summary()


def test_the_retry_job_stops_as_soon_as_anilist_goes_down_again(connected):
    defer_one(connected, rating_key="1", episode=1)
    defer_one(connected, rating_key="2", episode=2)
    connected.down_reads = True
    connected.trips_circuit = True

    report = asyncio.run(retry.retry_deferred(force=True))

    assert report.attempted == 1
    assert report.still_deferred == 1
    assert "stopped early" in report.summary()


def test_a_single_failed_event_can_be_retried_by_hand(connected):
    connected.down_reads = True
    connected.error = AniListUnavailable("boom")
    event_id = defer_one(connected)
    db.execute("UPDATE event SET status = 'error' WHERE id = ?", (event_id,))

    status, detail = asyncio.run(retry.retry_event(event_id))

    assert status == "would_write"
    assert event_row(event_id)["status"] == "would_write"


def test_only_deferred_or_failed_events_can_be_retried(connected):
    result = run(make_event(tvdb_id=72025, season=1, episode=1), connected)
    status, detail = asyncio.run(retry.retry_event(result.event_id))
    assert status is None
    assert "only deferred or failed" in detail
