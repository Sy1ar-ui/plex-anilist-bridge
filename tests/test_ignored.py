"""Dropping a show.

A correction says "this Plex show is that AniList title". Dropping says the
opposite -- "this is not anime, stop asking" -- which is what a shared Plex
server and the occasional mis-filed episode need. It must never write
anything, and it must survive the next episode rather than being a one-shot
dismiss.
"""
from __future__ import annotations

import asyncio

from app import db
from app.models import ShowIds
from app.sync import ignored
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList, make_event

LAST_WEEK_TONIGHT = "plex://show/5d9c0839e98e47001eb0ab81"


def show(**kw) -> ShowIds:
    return ShowIds(**{"plex_guid": LAST_WEEK_TONIGHT, "tvdb_id": 278518,
                      "title": "Last Week Tonight with John Oliver", **kw})


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------
def test_a_dropped_show_matches_by_plex_guid(temp_db):
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    assert ignored.is_ignored(show()) is True
    assert ignored.is_ignored(show(plex_guid="plex://show/other", tvdb_id=1)) is False


def test_a_dropped_show_matches_by_tvdb_id(temp_db):
    """For an item Plex gave no GUID, the TVDB id is the stable identity."""
    ignored.add(scope_type=ignored.SCOPE_TVDB, scope_key="278518", title="LWT")
    assert ignored.is_ignored(show(plex_guid=None)) is True
    assert ignored.is_ignored(show(plex_guid=None, tvdb_id=999)) is False


def test_dropping_twice_is_harmless(temp_db):
    assert ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT") is True
    assert ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT") is False
    assert ignored.count() == 1


def test_undropping(temp_db):
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    row = ignored.listing()[0]
    assert ignored.remove(row["id"]) is True
    assert ignored.is_ignored(show()) is False
    assert ignored.remove(row["id"]) is False


def test_a_show_with_no_usable_identity_cannot_be_dropped(temp_db):
    assert ignored.is_ignored(ShowIds(title="only a title")) is False


# --------------------------------------------------------------------------
# through the pipeline
# --------------------------------------------------------------------------
def entry():
    from app.models import AniListEntryState

    return AniListEntryState(anilist_id=290, exists=True, status="CURRENT", progress=0,
                             total_episodes=13, media_status="FINISHED", title="Crest")


def run(event, client):
    return asyncio.run(handle_watch_event(event, anilist=client, gate=WriteGate(dry_run=False)))


def test_a_dropped_show_never_reaches_the_matcher(datasets, settings):
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    client = FakeAniList(entry())

    event = make_event(tvdb_id=278518, plex_guid=LAST_WEEK_TONIGHT, season=13, episode=29,
                       title="Last Week Tonight with John Oliver")
    result = run(event, client)

    assert result.status == "skipped"
    assert "dropped" in result.detail
    assert client.reads == 0
    assert client.saved == []


def test_the_next_episode_of_a_dropped_show_is_skipped_too(datasets, settings):
    """The difference from a one-shot dismiss: it does not come back."""
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    client = FakeAniList(entry())

    for episode in (29, 30, 31):
        result = run(
            make_event(tvdb_id=278518, plex_guid=LAST_WEEK_TONIGHT, season=13,
                       episode=episode, rating_key=str(episode)),
            client,
        )
        assert result.status == "skipped"

    assert db.query_one("SELECT COUNT(*) AS n FROM event WHERE status = 'skipped'")["n"] == 3
    from app import repo

    assert repo.pending_review_count() == 0


def test_dropping_writes_nothing_anywhere(datasets, settings):
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    client = FakeAniList(entry())
    run(make_event(tvdb_id=278518, plex_guid=LAST_WEEK_TONIGHT, season=13, episode=29), client)

    assert db.query_one("SELECT COUNT(*) AS n FROM write_log")["n"] == 0


def test_an_undropped_show_is_matched_again(datasets, settings):
    """Crest of the Stars is in the fixture dataset, so it matches once allowed."""
    guid = "plex://show/crest"
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=guid, title="Crest")
    client = FakeAniList(entry())

    first = run(make_event(tvdb_id=72025, plex_guid=guid, season=1, episode=1, rating_key="a"), client)
    assert first.status == "skipped"

    ignored.remove(ignored.listing()[0]["id"])
    second = run(make_event(tvdb_id=72025, plex_guid=guid, season=1, episode=1, rating_key="b"), client)
    assert second.status == "written"


def test_a_show_that_is_not_dropped_is_unaffected(datasets, settings):
    ignored.add(scope_type=ignored.SCOPE_SHOW, scope_key=LAST_WEEK_TONIGHT, title="LWT")
    client = FakeAniList(entry())
    result = run(make_event(tvdb_id=72025, plex_guid="plex://show/crest", season=1, episode=1), client)
    assert result.status == "written"
