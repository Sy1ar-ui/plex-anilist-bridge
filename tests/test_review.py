"""The review queue: grouping, the range editor, corrections, and re-running."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.mapping import engine
from app.models import AniListEntryState
from app.sync import review
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList, seed_media, seed_seasons
from tests.conftest import make_event as _make_event

SHOW = "plex://show/0000000000000000000000aa"
OTHER = "plex://show/0000000000000000000000bb"
MOVIE = "plex://movie/0000000000000000000000cc"
NOWHERE_TVDB = 9_999_001  # in neither dataset slice


def make_event(**kw):
    from dataclasses import replace

    kw.setdefault("plex_guid", SHOW)
    kw.setdefault("tvdb_id", NOWHERE_TVDB)
    kw.setdefault("title", "Obscure Show")
    kw.setdefault("rating_key", f"rk-{kw.get('season', 1)}-{kw.get('episode', 1)}-{kw['plex_guid']}")
    return replace(_make_event(**kw), received_at=datetime.now(UTC))


def entry(anilist_id=5000, total=12, **kw) -> AniListEntryState:
    defaults = {
        "anilist_id": anilist_id, "exists": True, "status": "CURRENT", "progress": 0,
        "total_episodes": total, "media_status": "FINISHED", "title": "Obscure Show",
    }
    defaults.update(kw)
    return AniListEntryState(**defaults)


def run(event, client=None, *, dry_run=True):
    return asyncio.run(
        handle_watch_event(event, anilist=client or FakeAniList(entry()), gate=WriteGate(dry_run=dry_run))
    )


def unmatched(*episodes, season=1, **kw) -> list[int]:
    ids = []
    for ep in episodes:
        result = run(make_event(season=season, episode=ep, **kw))
        assert result.status == "unmatched", result.detail
        ids.append(result.event_id)
    return ids


def event_row(event_id: int) -> dict:
    return dict(db.query_one("SELECT * FROM event WHERE id = ?", (event_id,)))


# --------------------------------------------------------------------------
# small pieces
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("12345", 12345),
        (" 12345 ", 12345),
        ("https://anilist.co/anime/21355/ReZERO/", 21355),
        ("anilist.co/anime/7", 7),
        ("", None),
        ("abc", None),
        ("0", None),
        ("https://myanimelist.net/anime/123", None),
    ],
)
def test_anilist_ids_and_links_are_understood(text, expected):
    assert review.parse_anilist_ref(text) == expected


def test_a_range_is_stored_as_the_engines_offset():
    spec = review.parse_range("14", "", "1")
    assert (spec.ep_from, spec.ep_to, spec.offset) == (14, None, 13)
    assert spec.covers(14) and spec.covers(200) and not spec.covers(13)


@pytest.mark.parametrize("ep_from,ep_to,start", [("", "", "1"), ("0", "", "1"), ("5", "3", "1"), ("1", "", "x")])
def test_a_bad_range_is_refused(ep_from, ep_to, start):
    with pytest.raises(ValueError):
        review.parse_range(ep_from, ep_to, start)


def test_the_preview_flags_episodes_past_the_end():
    rows = review.preview(review.parse_range("10", "15", "1"), season_end=None, total=4, watched=[11])
    assert [(r["plex"], r["entry"]) for r in rows] == [(10, 1), (11, 2), (12, 3), (13, 4), (14, 5), (15, 6)]
    assert [r["past_end"] for r in rows] == [False, False, False, False, True, True]
    assert [r["watched"] for r in rows] == [False, True, False, False, False, False]


def test_the_preview_runs_to_the_end_of_the_season_when_known():
    rows = review.preview(review.parse_range("1", "", "1"), season_end=8, total=None, watched=[2])
    assert rows[-1]["plex"] == 8


def test_the_preview_stops_at_the_last_watched_episode_otherwise():
    rows = review.preview(review.parse_range("1", "", "1"), season_end=None, total=None, watched=[2, 5])
    assert rows[-1]["plex"] == 5


def test_episode_lists_are_compacted():
    assert review.compact_ranges([7, 1, 2, 3, 5, 8]) == "1-3, 5, 7-8"
    assert review.compact_ranges([]) == ""


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------
def test_episodes_of_one_season_are_one_item(datasets, settings):
    unmatched(1, 2, 3, 5)
    unmatched(1, season=2)
    groups = review.open_groups()
    assert len(groups) == 2
    season_one = next(g for g in groups if g.season == 1)
    assert season_one.episode_text == "1-3, 5"
    assert len(season_one.event_ids) == 4
    assert season_one.tvdb_id == NOWHERE_TVDB
    assert review.open_count() == 2


def test_dismissing_writes_nothing_and_empties_the_group(datasets, settings):
    ids = unmatched(1, 2)
    review.dismiss(review.group_for_event(ids[0]))
    assert review.open_groups() == []
    assert {event_row(i)["status"] for i in ids} == {"skipped"}
    assert db.query_one("SELECT COUNT(*) AS n FROM write_log")["n"] == 0


def test_the_suggested_range_follows_a_candidates_offset():
    group = review.Group(
        key="k", event_ids=[1], title="t", media_kind="episode", season=2, episodes=[14],
        plex_guid=SHOW, tvdb_id=None, status="held", summary="", latest_id=1, latest_at=None,
    )
    assert review.suggest_range(group, {"ep_offset": 13}).ep_from == 14
    assert review.suggest_range(group, None).ep_from == 1


# --------------------------------------------------------------------------
# linking and re-running
# --------------------------------------------------------------------------
def test_linking_a_season_rewrites_its_events_and_later_ones(datasets, settings):
    ids = unmatched(1, 2, 3)
    group = review.group_for_event(ids[0])
    spec = review.parse_range("1", "", "1")
    review.save_override(group, anilist_id=5000, spec=spec)

    client = FakeAniList(entry())
    report = asyncio.run(review.reprocess(review.covered_events(group, spec), anilist=client))

    assert report.outcomes == {"would_write": 3}
    assert review.open_groups() == []
    assert event_row(ids[2])["confidence"] == "override"
    assert event_row(ids[2])["entry_episode"] == 3

    # The next episode needs no review at all.
    later = run(make_event(episode=4), client)
    assert later.status == "would_write"
    assert later.match.confidence == "override"


def test_a_split_season_is_two_corrections(datasets, settings):
    """Plex S01E14 onwards is the second AniList part, starting at its episode 1."""
    seed_media(6000, episodes=13)
    ids = unmatched(13, 14, 15)
    group = review.group_for_event(ids[0])
    review.save_override(group, anilist_id=5000, spec=review.parse_range("1", "13", "1"))
    review.save_override(group, anilist_id=6000, spec=review.parse_range("14", "", "1"))

    assert engine.resolve(make_event(episode=13)).candidate.entry_episode == 13
    second = engine.resolve(make_event(episode=15)).candidate
    assert (second.anilist_id, second.entry_episode) == (6000, 2)


def test_only_events_inside_the_range_are_rerun(datasets, settings):
    ids = unmatched(1, 2, 14)
    group = review.group_for_event(ids[0])
    covered = review.covered_events(group, review.parse_range("14", "", "1"))
    assert covered == [ids[2]]


def test_a_tvdb_wide_correction_applies_to_another_copy_of_the_show(datasets, settings):
    ids = unmatched(1)
    review.save_override(
        review.group_for_event(ids[0]), anilist_id=5000,
        spec=review.parse_range("1", "", "1"), scope=review.SCOPE_TVDB,
    )
    elsewhere = engine.resolve(make_event(plex_guid=OTHER, episode=2))
    assert elsewhere.confidence == "override"


def test_saving_the_same_start_again_replaces_it(datasets, settings):
    ids = unmatched(1)
    group = review.group_for_event(ids[0])
    review.save_override(group, anilist_id=5000, spec=review.parse_range("1", "", "1"))
    review.save_override(group, anilist_id=5001, spec=review.parse_range("1", "", "1"))
    overrides = review.list_overrides()
    assert [o["anilist_id"] for o in overrides] == [5001]
    assert overrides[0]["show_title"] == "Obscure Show"


def test_a_movie_is_linked_as_one_item(datasets, settings):
    result = run(make_event(plex_guid=MOVIE, media_kind="movie", season=None, episode=None,
                            tvdb_id=None, title="Obscure Film"))
    assert result.status == "unmatched"
    group = review.group_for_event(result.event_id)
    assert group.media_kind == "movie"
    review.save_override(group, anilist_id=7000, spec=review.parse_range("3", "", "9"))

    [override] = review.list_overrides()
    assert (override["season"], override["ep_from"], override["ep_offset"]) == (1, 1, 0)
    rerun = asyncio.run(review.reprocess(group.event_ids, anilist=FakeAniList(entry(7000, total=1))))
    assert sum(rerun.outcomes.values()) == 1
    assert event_row(result.event_id)["confidence"] == "override"


def test_deleting_a_correction_lets_the_datasets_decide_again(datasets, settings):
    ids = unmatched(1)
    review.save_override(review.group_for_event(ids[0]), anilist_id=5000,
                         spec=review.parse_range("1", "", "1"))
    [override] = review.list_overrides()
    assert review.delete_override(override["id"])
    assert engine.resolve(make_event(episode=1)).confidence == "unmatched"


def test_rerunning_respects_the_rating_hold(datasets, settings):
    """A correction does not bypass the pipeline's rules."""
    seed_seasons(SHOW, {1: 12})
    ids = unmatched(12)
    group = review.group_for_event(ids[0])
    review.save_override(group, anilist_id=5000, spec=review.parse_range("1", "", "1"))
    client = FakeAniList(entry(progress=11))
    report = asyncio.run(review.reprocess(group.event_ids, anilist=client))
    assert report.outcomes == {"awaiting_rating": 1}


# --------------------------------------------------------------------------
# the pages
# --------------------------------------------------------------------------
@pytest.fixture
def ui(datasets, settings):
    return TestClient(create_app())


def test_the_review_page_lists_groups_and_counts_them_in_the_nav(ui):
    unmatched(1, 2, 3)
    page = ui.get("/review")
    assert page.status_code == 200
    assert "Obscure Show" in page.text
    assert "episodes 1-3" in page.text
    assert 'class="nav-count"' in ui.get("/").text


def test_an_empty_queue_says_so(ui):
    assert "Nothing to review" in ui.get("/review").text


def test_the_link_page_previews_a_range(ui):
    seed_media(5000, episodes=2, title="Obscure Show TV")
    ids = unmatched(1, 2, 3)
    page = ui.get(f"/review/{ids[0]}?anilist=5000&ep_from=1&ep_to=&start=1")
    assert page.status_code == 200
    assert "Obscure Show TV" in page.text
    assert 'id="ep-strip"' in page.text
    assert page.text.count('class="ep ') == 3
    # Episode 3 is waiting and lands past the end of a 2-episode title.
    assert page.text.count('class="ep ep-watched "') == 2   # episodes 1 and 2: fine
    assert page.text.count('class="ep ep-watched ep-bad"') == 1  # episode 3: flagged
    assert 'id="ep-warning" hidden' not in page.text


def test_search_without_anilist_points_to_pasting_an_id(ui):
    ids = unmatched(1)
    page = ui.get(f"/review/{ids[0]}?q=obscure")
    assert "Connect AniList to search" in page.text


def test_a_bad_anilist_reference_is_reported(ui):
    ids = unmatched(1)
    page = ui.get(f"/review/{ids[0]}?anilist=not-an-id")
    assert "not an AniList id" in page.text


def test_saving_without_anilist_keeps_the_events_and_offers_a_rerun(ui):
    ids = unmatched(1, 2)
    response = ui.post(
        f"/review/{ids[0]}/link",
        data={"anilist": "https://anilist.co/anime/5000", "ep_from": "1", "ep_to": "", "start": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Correction+saved" in response.headers["location"]
    assert len(review.list_overrides()) == 1
    page = ui.get("/review")
    assert "A correction covers all of these now" in page.text
    assert f'action="/review/{ids[1]}/rerun"' in page.text


def test_saving_a_bad_range_is_refused(ui):
    ids = unmatched(1)
    response = ui.post(
        f"/review/{ids[0]}/link",
        data={"anilist": "5000", "ep_from": "5", "ep_to": "2", "start": "1"},
        follow_redirects=False,
    )
    assert "level=error" in response.headers["location"]
    assert review.list_overrides() == []


def test_the_event_page_links_to_the_fix(ui):
    ids = unmatched(1)
    page = ui.get(f"/activity/{ids[0]}")
    assert f'href="/review/{ids[0]}"' in page.text


def test_dismiss_and_delete_from_the_page(ui):
    ids = unmatched(1)
    ui.post(f"/review/{ids[0]}/link", data={"anilist": "5000"}, follow_redirects=False)
    [override] = review.list_overrides()
    ui.post(f"/review/corrections/{override['id']}/delete", follow_redirects=False)
    assert review.list_overrides() == []
    ui.post(f"/review/{ids[0]}/dismiss", follow_redirects=False)
    assert review.open_groups() == []


def test_waiting_episodes_outside_the_range_are_pointed_out(ui):
    ids = unmatched(12, 13)
    page = ui.get(f"/review/{ids[0]}?anilist=5000&ep_from=13&ep_to=&start=1")
    assert "Not in this range" in page.text
    assert 'id="ep-uncovered" hidden' not in page.text
    assert 'id="ep-uncovered-list">E12<' in page.text


def test_the_queue_says_how_much_a_correction_covers(ui):
    ids = unmatched(12, 13, 14)
    ui.post(f"/review/{ids[0]}/link", data={"anilist": "5000", "ep_from": "13", "start": "1"},
            follow_redirects=False)
    assert "A correction covers 2 of 3 now" in ui.get("/review").text


@pytest.fixture
def connected_ui(ui, monkeypatch):
    """AniList connected: the UI's client factory hands out a fake."""
    from app.web import routes_ui

    client = FakeAniList(entry())

    class Factory:
        @staticmethod
        def from_db(**_kw):
            return client

    monkeypatch.setattr(routes_ui, "AniListClient", Factory)
    return ui, client


def test_save_and_rerun_over_http(connected_ui):
    ui, _client = connected_ui
    ids = unmatched(12, 13, 14)
    response = ui.post(
        f"/review/{ids[0]}/link",
        data={"anilist": "5000", "ep_from": "13", "ep_to": "", "start": "1"},
        follow_redirects=False,
    )
    assert "level=ok" in response.headers["location"]
    assert "Re-ran+2+event" in response.headers["location"]
    assert event_row(ids[0])["status"] == "unmatched"      # E12 is outside the range
    assert event_row(ids[1])["status"] == "would_write"    # dry run
    assert event_row(ids[2])["entry_episode"] == 2
    [group] = review.open_groups()
    assert group.episodes == [12]


def test_rerun_only_sends_covered_episodes(connected_ui):
    ui, client = connected_ui
    ids = unmatched(12, 13)
    review.save_override(review.group_for_event(ids[0]), anilist_id=5000,
                         spec=review.parse_range("13", "", "1"))
    reads_before = client.reads
    ui.post(f"/review/{ids[0]}/rerun", follow_redirects=False)
    assert client.reads - reads_before == 1
    assert event_row(ids[0])["status"] == "unmatched"
    assert event_row(ids[1])["status"] == "would_write"
