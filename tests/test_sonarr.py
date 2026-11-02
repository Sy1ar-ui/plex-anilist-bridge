"""Sonarr as an optional source of the TVDB id Plex did not give."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, repo, settings_store, sonarr
from app.main import create_app
from app.mapping import engine
from app.models import AniListEntryState, ShowIds
from app.plex.client import folder_name
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event

SONARR = "http://sonarr.lan:8989"
SERIES = [
    {"id": 1, "title": "Crest of the Stars", "year": 1999, "tvdbId": 72025, "tmdbId": 26209,
     "imdbId": "tt0251478", "path": "/tv/anime/Crest of the Stars (1999)",
     "alternateTitles": [{"title": "Seikai no Monshou"}]},
    {"id": 2, "title": "Twin", "year": 2010, "tvdbId": 900001, "path": "/tv/Twin (2010)"},
    {"id": 3, "title": "Twin", "year": 2010, "tvdbId": 900002, "path": "/tv/Twin (2010) [JP]"},
    {"id": 4, "title": "No Tvdb Yet", "year": 2026, "tvdbId": 0, "path": "/tv/No Tvdb Yet"},
]


class SonarrServer:
    def __init__(self, series=SERIES, status=200):
        self.series = series
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        if request.headers.get("X-Api-Key") != "key":
            return httpx.Response(401, json={"error": "Unauthorized"})
        if self.status != 200:
            return httpx.Response(self.status, text="<html>oops</html>")
        if request.url.path.endswith("/system/status"):
            return httpx.Response(200, json={"version": "4.0.11"})
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json=self.series)
        return httpx.Response(404)


@pytest.fixture
def server(monkeypatch):
    fake = SonarrServer()
    real = httpx.AsyncClient

    def factory(*_a, **kw):
        return real(transport=httpx.MockTransport(fake), **kw)

    monkeypatch.setattr(sonarr.httpx, "AsyncClient", factory)
    return fake


@pytest.fixture
def loaded(datasets, settings, server):
    settings_store.set_many({"sonarr_url": SONARR, "sonarr_api_key": "key"})
    assert asyncio.run(sonarr.refresh()) == 4
    return server


def show(**kw) -> ShowIds:
    defaults = {"plex_guid": "plex://show/nopeids", "title": "Something Else"}
    defaults.update(kw)
    return ShowIds(**defaults)


def make_event(**kw):
    from dataclasses import replace

    return replace(_make_event(**kw), received_at=datetime.now(UTC))


# --------------------------------------------------------------------------
# reading Sonarr
# --------------------------------------------------------------------------
@pytest.mark.parametrize("locations,expected", [
    (["/data/media/anime/Crest of the Stars (1999)"], "Crest of the Stars (1999)"),
    (["/tv/Show/"], "Show"),
    (["D:\\Media\\Anime\\Show Name"], "Show Name"),
    ([], None), (None, None),
])
def test_the_plex_folder_name_ignores_the_mount(locations, expected):
    assert folder_name(locations) == expected


def test_the_series_list_is_cached(loaded):
    rows = {r["sonarr_id"]: dict(r) for r in db.query("SELECT * FROM sonarr_series")}
    assert rows[1]["folder"] == "crest of the stars (1999)"
    assert "seikainomonshou" in rows[1]["alt_titles"]
    assert rows[4]["tvdb_id"] is None
    assert sonarr.status()["series"] == 4


def test_a_refresh_replaces_the_old_list(loaded):
    loaded.series = SERIES[:1]
    assert asyncio.run(sonarr.refresh()) == 1
    assert sonarr.status()["series"] == 1


def test_a_wrong_key_is_reported(temp_db, settings, server):
    with pytest.raises(sonarr.SonarrError, match="API key"):
        asyncio.run(sonarr.check(SONARR, "wrong"))


def test_a_wrong_url_base_is_reported(temp_db, settings, server):
    server.status = 200
    server.series = "not a list"
    settings_store.set_many({"sonarr_url": SONARR, "sonarr_api_key": "key"})
    with pytest.raises(sonarr.SonarrError):
        asyncio.run(sonarr.refresh())


def test_it_is_not_configured_without_both_url_and_key(temp_db, settings):
    assert sonarr.configured() is None
    settings_store.set_many({"sonarr_url": "ftp://x", "sonarr_api_key": "k"})
    assert sonarr.configured() is None


# --------------------------------------------------------------------------
# the lookup
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ids,basis", [
    ({"imdb_id": "tt0251478"}, "imdb"),
    ({"tmdb_id": 26209}, "tmdb"),
    ({"folder": "Crest of the Stars (1999)"}, "folder"),
    ({"title": "Crest of the Stars", "year": 1999}, "title"),
    ({"title": "Seikai no Monshou"}, "title"),
])
def test_a_show_is_found_by_each_kind_of_evidence(loaded, ids, basis):
    found, why = sonarr.lookup(show(**ids))
    assert (found.tvdb_id, found.basis) == (72025, basis)
    assert found.certain is (basis != "title")
    assert "Sonarr recovered TVDB 72025" in why


def test_a_title_with_the_wrong_year_is_not_a_match(loaded):
    found, _ = sonarr.lookup(show(title="Crest of the Stars", year=2005))
    assert found is None


def test_ambiguity_is_never_guessed_through(loaded):
    found, why = sonarr.lookup(show(title="Twin", year=2010))
    assert found is None
    assert "needs a human" in why


def test_stronger_evidence_wins(loaded):
    found, _ = sonarr.lookup(show(imdb_id="tt0251478", title="Twin"))
    assert found.basis == "imdb"


def test_without_a_list_it_says_so(datasets, settings):
    found, why = sonarr.lookup(show(title="Crest of the Stars"))
    assert found is None and "not set up" in why


# --------------------------------------------------------------------------
# in the mapping engine
# --------------------------------------------------------------------------
def test_a_recovered_tvdb_id_lets_the_dataset_match(loaded):
    # make_event has no folder parameter; give the show its folder directly.
    from dataclasses import replace

    event = make_event(plex_guid="plex://show/x", title="Crest", season=1, episode=3)
    event = replace(event, show=replace(event.show, folder="Crest of the Stars (1999)"))
    result = engine.resolve(event)
    assert result.confidence == "confident"
    assert result.candidate.anilist_id == 290
    assert result.summary.startswith("Sonarr recovered TVDB 72025")
    assert any(s.layer == "sonarr" and s.outcome == "hit" for s in result.steps)


def test_a_recovery_by_title_is_only_uncertain(loaded):
    result = engine.resolve(make_event(plex_guid="plex://show/x", title="Crest of the Stars",
                                       season=1, episode=3))
    assert result.confidence == "uncertain"
    assert "title only" in result.summary


def test_sonarr_is_not_consulted_when_plex_gave_a_tvdb_id(loaded):
    result = engine.resolve(make_event(tvdb_id=9_999_001, plex_guid="plex://show/y", title="Crest of the Stars",
                                       season=1, episode=1))
    assert result.confidence == "unmatched"
    assert any(s.layer == "sonarr" and s.outcome == "skipped" for s in result.steps)


def test_an_ambiguous_lookup_is_recorded_as_rejected(loaded):
    result = engine.resolve(make_event(plex_guid="plex://show/z", title="Twin", season=1, episode=1))
    assert result.confidence == "unmatched"
    assert any(s.layer == "sonarr" and s.outcome == "rejected" for s in result.steps)


def test_the_pipeline_holds_a_title_recovery_and_writes_a_folder_recovery(loaded):
    from dataclasses import replace

    state = AniListEntryState(anilist_id=290, exists=True, status="CURRENT", progress=0,
                              total_episodes=13, media_status="FINISHED")
    held = asyncio.run(handle_watch_event(
        make_event(plex_guid="plex://show/a", title="Crest of the Stars", season=1, episode=1, rating_key="1"),
        anilist=FakeAniList(state, search_results=[]), gate=WriteGate(dry_run=True)))
    assert held.status == "held"

    event = make_event(plex_guid="plex://show/b", title="Crest", season=1, episode=2, rating_key="2")
    event = replace(event, show=replace(event.show, folder="Crest of the Stars (1999)"))
    written = asyncio.run(handle_watch_event(event, anilist=FakeAniList(state), gate=WriteGate(dry_run=True)))
    assert written.status == "would_write"


def test_the_folder_is_stored_with_the_show(datasets, settings):
    from dataclasses import replace

    event = make_event(plex_guid="plex://show/f", title="F", season=1, episode=1)
    event = replace(event, show=replace(event.show, folder="F Folder"))
    repo.upsert_show(event.show, rating_key="9", section_id=None)
    row = db.query_one("SELECT folder FROM show WHERE plex_guid = 'plex://show/f'")
    assert row["folder"] == "F Folder"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@pytest.fixture
def ui(datasets, settings):
    settings_store.set("setup_complete", True)
    return TestClient(create_app())


def test_save_and_test_stores_and_reads_the_list(ui, server):
    response = ui.post("/settings/sonarr", data={"sonarr_url": SONARR + "/", "sonarr_api_key": "key"},
                       follow_redirects=False)
    assert "Connected+to+Sonarr+4.0.11" in response.headers["location"]
    assert settings_store.get("sonarr_url") == SONARR
    assert sonarr.status()["series"] == 4
    assert "4 series" in ui.get("/system").text


def test_a_bad_key_is_not_saved(ui, server):
    response = ui.post("/settings/sonarr", data={"sonarr_url": SONARR, "sonarr_api_key": "wrong"},
                       follow_redirects=False)
    assert "level=error" in response.headers["location"]
    assert settings_store.get("sonarr_url") == ""


def test_the_key_is_kept_when_left_blank(ui, server):
    ui.post("/settings/sonarr", data={"sonarr_url": SONARR, "sonarr_api_key": "key"})
    response = ui.post("/settings/sonarr", data={"sonarr_url": SONARR, "sonarr_api_key": ""},
                       follow_redirects=False)
    assert "level=ok" in response.headers["location"]
    assert 'value="key"' not in ui.get("/settings").text        # never echoed back


def test_remove_forgets_sonarr(ui, server):
    ui.post("/settings/sonarr", data={"sonarr_url": SONARR, "sonarr_api_key": "key"})
    ui.post("/settings/sonarr", data={"action": "remove"})
    assert sonarr.configured() is None and sonarr.status()["series"] == 0
