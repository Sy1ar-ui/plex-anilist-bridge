"""Shared test setup.

Every test runs against a throwaway SQLite database and the fixture slices in
``tests/fixtures``. Nothing in the suite touches the network: the datasets come
from disk and the API clients are replaced with stubs.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import db, settings_store
from app.datasets import community, fribb
from app.mapping.seasons import SeasonInfo, save_season_counts
from app.models import ShowIds, WatchEvent

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_anilist_health():
    """The circuit breaker is process-wide; no test may inherit another's outage."""
    from app.anilist.health import health

    health().reset()
    yield health()
    health().reset()


@pytest.fixture
def temp_db(tmp_path):
    db.set_db_path(tmp_path / "test.db")
    db.migrate()
    return tmp_path / "test.db"


@pytest.fixture
def datasets(temp_db):
    """A database with both mapping datasets loaded from the fixture slices."""
    fribb.load(fribb.parse_bytes((FIXTURES / "fribb_slice.json").read_bytes()))
    community.load(
        community.parse_yaml(
            (FIXTURES / "community_slice.yaml").read_text(encoding="utf-8"),
            "community_slice.yaml",
        )
    )
    return temp_db


@pytest.fixture
def settings(temp_db):
    """Explicit defaults, so a test never depends on another test's settings."""
    settings_store.set_many(
        {
            "dry_run": True,
            "sync_enabled": True,
            "skip_specials": True,
            "add_untracked": True,
            "uncertain_action": "hold",
            "rewatch_action": "ignore",
            "set_dates": True,
            "rate_before_complete": True,
        }
    )
    return settings_store


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------
def make_event(
    *,
    season: int | None = 1,
    episode: int | None = 1,
    tvdb_id: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
    plex_guid: str | None = None,
    title: str | None = "A Show",
    media_kind: str = "episode",
    account_id: int | None = 1,
    rating_key: str | None = "1000",
    source: str = "webhook",
    viewed_at: datetime | None = None,
) -> WatchEvent:
    return WatchEvent(
        source=source,
        received_at=datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
        viewed_at=viewed_at,
        show=ShowIds(
            plex_guid=plex_guid,
            tvdb_id=tvdb_id,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            title=title,
        ),
        media_kind=media_kind,
        season=season,
        episode=episode,
        plex_account_id=account_id,
        rating_key=rating_key,
        show_rating_key="900",
    )


def seed_seasons(plex_guid: str, counts: dict[int, int], *, gaps: set[int] | None = None) -> None:
    """Record per-season episode counts as if they had been read from Plex."""
    gaps = gaps or set()
    save_season_counts(
        plex_guid,
        [
            SeasonInfo(
                season=season,
                episode_count=count,
                min_episode=1,
                max_episode=count if season not in gaps else count + 1,
                contiguous=season not in gaps,
            )
            for season, count in sorted(counts.items())
        ],
    )


def seed_media(anilist_id: int, *, episodes: int | None = None, title: str = "Cached Title") -> None:
    db.execute(
        "INSERT INTO anilist_media_cache (anilist_id, title_romaji, title_english,"
        " episodes, fetched_at) VALUES (?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT (anilist_id) DO UPDATE SET episodes = excluded.episodes",
        (anilist_id, title, title, episodes),
    )


def add_override(
    *,
    scope_key: str,
    season: int,
    ep_from: int,
    anilist_id: int,
    ep_offset: int = 0,
    ep_to: int | None = None,
    scope_type: str = "plex_guid",
) -> None:
    db.execute(
        "INSERT INTO mapping_override (scope_type, scope_key, season, ep_from, ep_to,"
        " anilist_id, ep_offset, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'test', datetime('now'))",
        (scope_type, scope_key, season, ep_from, ep_to, anilist_id, ep_offset),
    )


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------
class FakeAniList:
    """Stands in for AniListClient. Counts writes so replays can be asserted."""

    def __init__(self, state, media=None, search_results=None):
        self.state = state
        self.media = media or {"id": state.anilist_id, "episodes": state.total_episodes}
        self.search_results = search_results or []
        self.saved: list[dict] = []
        self.reads = 0

    async def media_with_entry(self, anilist_id: int):
        self.reads += 1
        return self.state, self.media

    async def save_entry(self, variables: dict):
        self.saved.append(dict(variables))
        return {"id": 1, **variables}

    async def save_rated_completion(self, variables: dict):
        self.saved.append(dict(variables))
        return {"id": 1, **variables}

    async def search(self, title: str, per_page: int = 10):
        return list(self.search_results)

    async def __aexit__(self, *exc):
        return None
