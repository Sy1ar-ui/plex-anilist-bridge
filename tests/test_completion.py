"""Completing a series only once it has been rated.

The scenario this exists for: the final episode is watched away from home on
2 November, the rating is given on the 4th, and AniList must say the series
was completed on the 2nd -- with the entry reading 12/13 in between.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime

import pytest

from app import db, settings_store, tokens
from app.anilist.client import AniListClient, AniListError, AniListUnavailable
from app.anilist.rules import (
    STATUS_COMPLETED,
    STATUS_CURRENT,
    STATUS_REPEATING,
    RuleSettings,
    plan_anilist_write,
    plan_rated_completion,
    score_to_raw,
)
from app.models import AniListEntryState
from app.sync import completion, retry
from app.sync.pipeline import handle_watch_event, watched_on
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event

FINISHED_ON = datetime(2026, 11, 2, 21, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def utc_local_time(monkeypatch):
    """Watch dates are local dates; pin the zone so the tests mean one thing."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def make_event(**kw):
    """Received now (deferral gives up on old events), watched on 2 November."""
    from dataclasses import replace

    kw.setdefault("viewed_at", FINISHED_ON)
    return replace(_make_event(**kw), received_at=datetime.now(UTC))


def entry(**kw) -> AniListEntryState:
    defaults = {
        "anilist_id": 290,
        "exists": True,
        "status": STATUS_CURRENT,
        "progress": 12,
        "total_episodes": 13,
        "media_status": "FINISHED",
        "title": "Crest of the Stars",
    }
    defaults.update(kw)
    return AniListEntryState(**defaults)


class FlakyAniList(FakeAniList):
    def __init__(self, *args, down=False, **kw):
        super().__init__(*args, **kw)
        self.down = down

    async def media_with_entry(self, anilist_id: int):
        if self.down:
            raise AniListUnavailable("AniList returned HTTP 503")
        return await super().media_with_entry(anilist_id)


def fake(state: AniListEntryState | None = None, **kw) -> FlakyAniList:
    state = state or entry()
    return FlakyAniList(
        state,
        media={"id": 290, "episodes": 13, "title": {"romaji": "Crest of the Stars"}},
        **kw,
    )


def run(event, client, *, dry_run=False):
    return asyncio.run(
        handle_watch_event(event, anilist=client, gate=WriteGate(dry_run=dry_run))
    )


def finale(**kw):
    return make_event(tvdb_id=72025, season=1, episode=13, **kw)


def submit(pending_id, score, client, *, dry_run=False):
    return asyncio.run(
        completion.submit(pending_id, score, anilist=client, gate=WriteGate(dry_run=dry_run))
    )


def event_row(event_id: int) -> dict:
    return dict(db.query_one("SELECT * FROM event WHERE id = ?", (event_id,)))


def rules(**kw) -> RuleSettings:
    defaults = {"today": date(2026, 11, 2), "hold_completion_for_rating": True}
    defaults.update(kw)
    return RuleSettings(**defaults)


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------
def test_the_final_episode_is_held_one_short():
    plan = plan_anilist_write(entry(progress=9), 13, rules())
    assert plan.awaiting_rating
    assert plan.diff.variables["status"] == STATUS_CURRENT
    assert plan.diff.variables["progress"] == 12
    assert "completedAt" not in plan.diff.variables


def test_nothing_is_written_when_already_one_short():
    plan = plan_anilist_write(entry(progress=12), 13, rules())
    assert plan.awaiting_rating
    assert plan.action == "no_change"
    assert plan.diff is None
    assert "12/13" in plan.reason


def test_an_untracked_binge_to_the_end_is_added_one_short():
    plan = plan_anilist_write(entry(exists=False, status=None, progress=0), 13, rules())
    assert plan.awaiting_rating
    assert plan.diff.variables["progress"] == 12
    assert plan.diff.variables["status"] == STATUS_CURRENT
    assert plan.diff.variables["startedAt"] == {"year": 2026, "month": 11, "day": 2}


def test_a_still_airing_series_is_not_held():
    plan = plan_anilist_write(entry(media_status="RELEASING"), 13, rules())
    assert not plan.awaiting_rating
    assert plan.diff.variables["progress"] == 13


def test_finishing_a_rewatch_is_not_held():
    plan = plan_anilist_write(
        entry(status=STATUS_REPEATING, progress=12),
        13,
        rules(rewatch_action="repeating"),
    )
    assert not plan.awaiting_rating
    assert plan.diff.variables["status"] == STATUS_COMPLETED


def test_holding_can_be_turned_off():
    plan = plan_anilist_write(entry(), 13, rules(hold_completion_for_rating=False))
    assert not plan.awaiting_rating
    assert plan.diff.variables["status"] == STATUS_COMPLETED


def test_automatic_writes_still_never_carry_a_score():
    for state in (entry(progress=3), entry(exists=False, status=None, progress=0)):
        plan = plan_anilist_write(state, 13, rules())
        assert not {"score", "scoreRaw", "notes"} & set(plan.diff.variables)


def test_the_rated_completion_is_dated_when_the_final_episode_was_watched():
    plan = plan_rated_completion(
        entry(), total_episodes=13, completed_on=date(2026, 11, 2), score=8.5
    )
    assert plan.diff.variables == {
        "mediaId": 290,
        "status": STATUS_COMPLETED,
        "progress": 13,
        "scoreRaw": 85,
        "completedAt": {"year": 2026, "month": 11, "day": 2},
    }


def test_an_existing_completion_date_is_kept():
    plan = plan_rated_completion(
        entry(completed_at={"year": 2025, "month": 1, "day": 1}),
        total_episodes=13,
        completed_on=date(2026, 11, 2),
        score=7,
    )
    assert "completedAt" not in plan.diff.variables
    assert plan.diff.variables["scoreRaw"] == 70


def test_completing_without_a_rating_sends_no_score():
    plan = plan_rated_completion(
        entry(), total_episodes=13, completed_on=date(2026, 11, 2), score=None
    )
    assert "scoreRaw" not in plan.diff.variables
    assert plan.diff.variables["status"] == STATUS_COMPLETED


@pytest.mark.parametrize("bad", [0, -1, 10.5, 11])
def test_a_rating_must_be_out_of_ten(bad):
    with pytest.raises(ValueError):
        score_to_raw(bad)


@pytest.mark.parametrize("raw,expected", [("8", 8.0), ("7.5", 7.5), ("9,5", 9.5), ("10", 10.0)])
def test_ratings_from_the_form_are_parsed(raw, expected):
    assert completion.parse_score(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "0", "12"])
def test_bad_ratings_from_the_form_are_refused(raw):
    with pytest.raises(ValueError):
        completion.parse_score(raw)


# --------------------------------------------------------------------------
# the client keeps scores out of every other write
# --------------------------------------------------------------------------
def test_the_ordinary_write_still_refuses_a_score():
    client = AniListClient("token")
    with pytest.raises(AniListError):
        asyncio.run(client.save_entry({"mediaId": 1, "progress": 1, "scoreRaw": 80}))


@pytest.mark.parametrize(
    "variables",
    [
        {"mediaId": 1, "status": "COMPLETED", "scoreRaw": 80, "notes": "hi"},
        {"mediaId": 1, "status": "CURRENT", "scoreRaw": 80},
    ],
)
def test_the_rating_write_only_carries_a_score_with_completed(variables):
    client = AniListClient("token")
    with pytest.raises(AniListError):
        asyncio.run(client.save_rated_completion(variables))


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------
def test_finish_while_away_and_rate_two_days_later(datasets, settings):
    client = fake()
    result = run(finale(), client)

    assert result.status == "awaiting_rating"
    assert client.saved == []  # already at 12/13: nothing to write yet
    [pending] = completion.list_open()
    assert pending["completed_on"] == "2026-11-02"
    assert pending["total_episodes"] == 13
    assert completion.open_count() == 1

    outcome = submit(pending["id"], 8, client)

    assert outcome.status == completion.DONE
    assert client.saved == [
        {
            "mediaId": 290,
            "status": "COMPLETED",
            "progress": 13,
            "scoreRaw": 80,
            "completedAt": {"year": 2026, "month": 11, "day": 2},
        }
    ]
    assert completion.list_open() == []
    row = event_row(result.event_id)
    assert row["status"] == "written"
    assert "completed after rating" in row["detail"]


def test_a_binge_writes_progress_up_to_one_short(datasets, settings):
    client = fake(entry(progress=8))
    result = run(finale(), client)
    assert result.status == "awaiting_rating"
    assert client.saved == [{"mediaId": 290, "status": "CURRENT", "progress": 12}]


def test_watching_the_finale_again_keeps_the_first_date(datasets, settings):
    client = fake()
    run(finale(rating_key="a"), client)
    run(finale(rating_key="b", viewed_at=datetime(2026, 11, 3, 20, 0, tzinfo=UTC)), client)
    [pending] = completion.list_open()
    assert pending["completed_on"] == "2026-11-02"


def test_dry_run_holds_and_completes_without_sending_anything(datasets, settings):
    client = fake(entry(progress=8))
    result = run(finale(), client, dry_run=True)
    [pending] = completion.list_open()
    submit(pending["id"], 9.5, client, dry_run=True)

    assert client.saved == []
    writes = db.query("SELECT * FROM write_log WHERE event_id = ? ORDER BY id", (result.event_id,))
    assert [w["dry_run"] for w in writes] == [1, 1]
    assert '"scoreRaw": 95' in writes[1]["request_json"]
    assert event_row(result.event_id)["status"] == "would_write"


def test_complete_without_a_rating(datasets, settings):
    client = fake()
    run(finale(), client)
    [pending] = completion.list_open()
    submit(pending["id"], None, client)
    assert "scoreRaw" not in client.saved[0]
    assert client.saved[0]["status"] == "COMPLETED"


def test_dismissing_writes_nothing(datasets, settings):
    client = fake()
    result = run(finale(), client)
    [pending] = completion.list_open()
    completion.dismiss(pending["id"])
    assert client.saved == []
    assert completion.list_open() == []
    assert event_row(result.event_id)["status"] == "no_change"


def test_with_the_setting_off_the_finale_completes_at_once(datasets, settings):
    settings_store.set("rate_before_complete", False)
    client = fake()
    result = run(finale(), client)
    assert result.status == "written"
    assert client.saved[0]["status"] == "COMPLETED"
    assert client.saved[0]["completedAt"] == {"year": 2026, "month": 11, "day": 2}
    assert completion.list_open() == []


def test_dates_follow_the_watch_not_the_processing(datasets, settings):
    """A late event (deferred, or found by reconcile) is dated when it was watched."""
    settings_store.set("rate_before_complete", False)
    client = fake(entry(exists=False, status=None, progress=0))
    run(make_event(tvdb_id=72025, season=1, episode=1,
                   viewed_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC)), client)
    assert client.saved[0]["startedAt"] == {"year": 2026, "month": 10, "day": 30}


def test_the_watch_date_is_the_local_day(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    time.tzset()
    late_utc = make_event(viewed_at=datetime(2026, 11, 2, 13, 0, tzinfo=UTC))
    assert watched_on(late_utc) == date(2026, 11, 3)


# --------------------------------------------------------------------------
# AniList unavailable when the rating is submitted
# --------------------------------------------------------------------------
@pytest.fixture
def connected(datasets, settings, monkeypatch):
    tokens.save(tokens.ANILIST, access_token="token", account_name="me")
    client = fake()

    class Factory:
        @staticmethod
        def from_db(**_kw):
            return client

    monkeypatch.setattr(retry, "AniListClient", Factory)
    monkeypatch.setattr(retry.plex_client.PlexClient, "from_db", classmethod(
        lambda cls: (_ for _ in ()).throw(retry.PlexUnavailable("no plex in tests"))
    ))
    return client


def test_a_rating_is_kept_and_sent_once_anilist_is_back(connected):
    settings_store.set("dry_run", False)
    run(finale(), connected)
    [pending] = completion.list_open()

    connected.down = True
    outcome = submit(pending["id"], 8, connected)
    assert outcome.status == completion.QUEUED
    assert "Rating saved" in outcome.detail
    assert completion.get(pending["id"])["score"] == 8
    assert connected.saved == []

    connected.down = False
    report = asyncio.run(retry.retry_deferred(force=True))

    assert report.completions_done == 1
    assert connected.saved[0]["scoreRaw"] == 80
    assert connected.saved[0]["completedAt"] == {"year": 2026, "month": 11, "day": 2}
    assert completion.get(pending["id"])["status"] == completion.DONE
    assert "rated completion" in report.summary()


def test_a_queued_rating_waits_for_its_backoff(connected):
    run(finale(), connected)
    [pending] = completion.list_open()
    connected.down = True
    submit(pending["id"], 8, connected)
    connected.down = False

    report = asyncio.run(retry.retry_deferred())
    assert report.completions_attempted == 0
    assert connected.saved == []
