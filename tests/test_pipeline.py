"""End-to-end pipeline behaviour: dry-run, idempotency, holding and skipping."""
from __future__ import annotations

import asyncio

from app import db, settings_store
from app.models import AniListEntryState
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList, make_event, seed_media

GINTAMA_GUID = "plex://show/5d9c08273c3f87001f348b54"


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


def fake(state: AniListEntryState, episodes: int | None = 13) -> FakeAniList:
    return FakeAniList(
        state,
        media={
            "id": state.anilist_id,
            "episodes": episodes,
            "idMal": 290,
            "title": {"romaji": "Crest of the Stars", "english": None},
        },
    )


def run(event, client, *, dry_run=True):
    return asyncio.run(
        handle_watch_event(event, anilist=client, gate=WriteGate(dry_run=dry_run))
    )


def write_log_rows() -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM write_log ORDER BY id")]


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------
def test_dry_run_sends_nothing_but_records_the_exact_payload(datasets, settings):
    client = fake(entry())
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client)

    assert result.status == "would_write"
    assert client.saved == []

    rows = write_log_rows()
    assert len(rows) == 1
    assert rows[0]["dry_run"] == 1
    assert rows[0]["result"] == "dry_run"
    assert '"progress": 1' in rows[0]["request_json"]


def test_a_real_run_writes(datasets, settings):
    client = fake(entry())
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client, dry_run=False)

    assert result.status == "written"
    assert len(client.saved) == 1
    sent = client.saved[0]
    assert sent["mediaId"] == 290
    assert sent["status"] == "CURRENT"
    assert sent["progress"] == 1
    assert write_log_rows()[0]["dry_run"] == 0


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------
def test_replaying_the_same_event_writes_once(datasets, settings):
    client = fake(entry())
    event = make_event(tvdb_id=72025, season=1, episode=1)

    first = run(event, client, dry_run=False)
    second = run(event, client, dry_run=False)

    assert first.status == "written"
    assert second.status == "duplicate"
    assert len(client.saved) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 1


def test_the_next_episode_is_a_separate_event(datasets, settings):
    client = fake(entry())
    run(make_event(tvdb_id=72025, season=1, episode=1, rating_key="1"), client, dry_run=False)
    client.state = entry(progress=1)
    run(make_event(tvdb_id=72025, season=1, episode=2, rating_key="2"), client, dry_run=False)

    assert [v["progress"] for v in client.saved] == [1, 2]
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 2


def test_a_webhook_and_a_reconcile_of_the_same_play_collapse(datasets, settings):
    """The reconcile sweep must not re-apply what the webhook already handled."""
    client = fake(entry())
    event = make_event(tvdb_id=72025, season=1, episode=1, rating_key="55")
    run(event, client, dry_run=False)

    from dataclasses import replace

    again = replace(event, source="reconcile")
    result = run(again, client, dry_run=False)

    assert result.status == "duplicate"
    assert len(client.saved) == 1


# --------------------------------------------------------------------------
# the rules, through the pipeline
# --------------------------------------------------------------------------
def test_nothing_is_written_when_anilist_is_already_ahead(datasets, settings):
    client = fake(entry(progress=10))
    result = run(make_event(tvdb_id=72025, season=1, episode=5), client, dry_run=False)

    assert result.status == "no_change"
    assert client.saved == []
    assert write_log_rows() == []


# --------------------------------------------------------------------------
# holding and skipping
# --------------------------------------------------------------------------
def test_specials_are_skipped_by_default(datasets, settings):
    client = fake(entry())
    result = run(make_event(tvdb_id=305089, season=0, episode=1), client)

    assert result.status == "skipped"
    assert "season 0" in result.detail
    assert client.reads == 0


def test_specials_can_be_enabled(datasets, settings):
    settings_store.set("skip_specials", False)
    client = fake(entry(anilist_id=100049, exists=False, status=None), episodes=1)
    result = run(make_event(tvdb_id=305089, season=0, episode=1), client)

    assert result.status != "skipped"
    assert client.reads == 1


def test_an_uncertain_match_is_held_for_review(datasets, settings):
    """Gintama season 2 continues AniList 918, but the episode counts are unknown."""
    client = fake(entry(anilist_id=918))
    result = run(make_event(plex_guid=GINTAMA_GUID, season=2, episode=1), client)

    assert result.status == "held"
    assert client.saved == []
    row = db.query_one("SELECT confidence FROM event ORDER BY id DESC LIMIT 1")
    assert row["confidence"] == "uncertain"


def test_an_uncertain_match_can_be_applied_and_flagged(datasets, settings):
    settings_store.set("uncertain_action", "apply_and_flag")
    client = fake(entry(anilist_id=918), episodes=None)
    result = run(make_event(plex_guid=GINTAMA_GUID, season=2, episode=1), client)

    assert result.status == "would_write"
    assert "flagged as uncertain" in result.detail


def test_an_unmatched_item_goes_to_review_and_is_never_guessed(datasets, settings):
    client = FakeAniList(entry(), search_results=[])
    result = run(make_event(tvdb_id=424242, season=1, episode=1), client)

    assert result.status == "unmatched"
    assert client.saved == []
    assert db.query_one("SELECT COUNT(*) AS n FROM event WHERE status='unmatched'")["n"] == 1


def test_syncing_can_be_turned_off_entirely(datasets, settings):
    settings_store.set("sync_enabled", False)
    client = fake(entry())
    result = run(make_event(tvdb_id=72025, season=1, episode=1), client)

    assert result.status == "skipped"
    assert client.reads == 0


# --------------------------------------------------------------------------
# the overflow re-check
# --------------------------------------------------------------------------
def test_an_episode_past_the_real_count_is_held_not_written(datasets, settings):
    """The engine's cache can be cold; the real count arrives with the entry."""
    client = fake(entry(total_episodes=3), episodes=3)
    result = run(make_event(tvdb_id=72025, season=1, episode=5), client, dry_run=False)

    assert result.status == "held"
    assert "past the 3 episodes" in result.detail
    assert client.saved == []


def test_episode_counts_are_cached_for_later_matches(datasets, settings):
    client = fake(entry())
    run(make_event(tvdb_id=72025, season=1, episode=1), client)

    row = db.query_one("SELECT episodes, title_romaji FROM anilist_media_cache WHERE anilist_id = 290")
    assert row["episodes"] == 13
    assert row["title_romaji"] == "Crest of the Stars"


# --------------------------------------------------------------------------
# the review queue counter the dashboard shows
# --------------------------------------------------------------------------
def test_pending_review_counts_held_and_unmatched(datasets, settings):
    client = fake(entry(anilist_id=918))
    run(make_event(plex_guid=GINTAMA_GUID, season=2, episode=1, rating_key="a"), client)
    run(make_event(tvdb_id=424242, season=1, episode=1, rating_key="b"), client)

    from app import repo

    assert repo.pending_review_count() == 2


def test_the_match_trace_is_stored_on_the_event(datasets, settings):
    seed_media(290, episodes=13, title="Crest of the Stars")
    client = fake(entry())
    run(make_event(tvdb_id=72025, season=1, episode=1), client)

    row = db.query_one("SELECT trace_json FROM event ORDER BY id DESC LIMIT 1")
    assert row["trace_json"]
    import json

    trace = json.loads(row["trace_json"])
    assert trace["chosen"]["anilist_id"] == 290
    assert any(step["layer"] == "fribb-dataset" for step in trace["steps"])
