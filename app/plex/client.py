"""Plex Media Server access.

Three jobs:

1. **Account list** for the setup picker, so the account whose activity counts
   is chosen from a list rather than typed as a display name.
2. **Enrichment** -- turn a webhook's ``grandparentRatingKey`` into the show's
   TVDB/TMDB/IMDb ids, which the webhook itself does not carry.
3. **Season episode counts**, needed for absolute-numbered shows and for Plex
   seasons that continue a single AniList entry.

python-plexapi is synchronous, so every call here is wrapped for use from async
code with :func:`run`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from plexapi.server import PlexServer

from app import db
from app.mapping.seasons import SeasonInfo
from app.models import ShowIds

log = logging.getLogger(__name__)

T = TypeVar("T")

_GUID_PATTERNS = (
    # Modern agents expose a Guid array: tvdb://81797, tmdb://37854, imdb://tt...
    (re.compile(r"^tvdb://(\d+)"), "tvdb"),
    (re.compile(r"^tmdb://(\d+)"), "tmdb"),
    (re.compile(r"^imdb://(tt\d+)"), "imdb"),
    # Legacy agents put the id in the primary guid instead.
    (re.compile(r"^com\.plexapp\.agents\.thetvdb://(\d+)"), "tvdb"),
    (re.compile(r"^com\.plexapp\.agents\.themoviedb://(\d+)"), "tmdb"),
    (re.compile(r"^com\.plexapp\.agents\.imdb://(tt\d+)"), "imdb"),
)


class PlexUnavailable(RuntimeError):
    """The Plex server could not be reached or refused the token."""


async def run(fn, *args, **kwargs):
    """Run a blocking plexapi call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@dataclass(frozen=True)
class PlexAccountInfo:
    id: int
    name: str
    thumb: str | None


@dataclass(frozen=True)
class PlexLibraryInfo:
    section_id: int
    title: str
    kind: str


@dataclass(frozen=True)
class EpisodeContext:
    """Everything the pipeline needs about one watched episode."""

    show: ShowIds
    season: int | None
    episode: int | None
    media_kind: str
    show_rating_key: str | None
    episode_title: str | None


def stored_server() -> tuple[str, str] | None:
    row = db.query_one("SELECT base_url, token FROM plex_server WHERE id = 1")
    if not row or not row["base_url"] or not row["token"]:
        return None
    return row["base_url"], row["token"]


def save_server(base_url: str, token: str, *, name: str | None, machine_id: str | None) -> None:
    db.execute(
        "INSERT INTO plex_server (id, base_url, token, machine_id, name, verified_at)"
        " VALUES (1, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT (id) DO UPDATE SET base_url = excluded.base_url,"
        "   token = excluded.token, machine_id = excluded.machine_id,"
        "   name = excluded.name, verified_at = excluded.verified_at",
        (base_url.rstrip("/"), token, machine_id, name),
    )


def configured_account_id() -> int | None:
    row = db.query_one("SELECT id FROM plex_account LIMIT 1")
    return int(row["id"]) if row else None


def save_account(account: PlexAccountInfo) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM plex_account")
        conn.execute(
            "INSERT INTO plex_account (id, name, thumb, chosen_at)"
            " VALUES (?, ?, ?, datetime('now'))",
            (account.id, account.name, account.thumb),
        )


def enabled_section_ids() -> set[int]:
    return {int(r["section_id"]) for r in db.query("SELECT section_id FROM library WHERE enabled = 1")}


# --------------------------------------------------------------------------
# guid parsing
# --------------------------------------------------------------------------
def extract_ids(primary_guid: str | None, guid_values: list[str]) -> dict[str, Any]:
    found: dict[str, Any] = {"tvdb_id": None, "tmdb_id": None, "imdb_id": None}
    for raw in [*guid_values, primary_guid or ""]:
        if not raw:
            continue
        for pattern, key in _GUID_PATTERNS:
            match = pattern.match(raw)
            if not match:
                continue
            value = match.group(1)
            if key == "imdb":
                found["imdb_id"] = found["imdb_id"] or value
            elif key == "tvdb":
                found["tvdb_id"] = found["tvdb_id"] or int(value)
            elif key == "tmdb":
                found["tmdb_id"] = found["tmdb_id"] or int(value)
    return found


def _guid_strings(item: Any) -> list[str]:
    values: list[str] = []
    for guid in getattr(item, "guids", None) or []:
        raw = getattr(guid, "id", None)
        if raw:
            values.append(str(raw))
    return values


def show_ids_from_item(item: Any) -> ShowIds:
    primary = getattr(item, "guid", None)
    ids = extract_ids(primary, _guid_strings(item))
    return ShowIds(
        plex_guid=str(primary) if primary else None,
        tvdb_id=ids["tvdb_id"],
        tmdb_id=ids["tmdb_id"],
        imdb_id=ids["imdb_id"],
        title=getattr(item, "title", None),
        year=getattr(item, "year", None),
    )


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------
class PlexClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._server: PlexServer | None = None

    @classmethod
    def from_db(cls) -> "PlexClient":
        stored = stored_server()
        if not stored:
            raise PlexUnavailable("no Plex server is configured yet")
        return cls(*stored)

    # -- connection --------------------------------------------------------
    def _connect(self) -> PlexServer:
        if self._server is None:
            try:
                self._server = PlexServer(self.base_url, self.token)
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
                raise PlexUnavailable(f"could not connect to {self.base_url}: {exc}") from exc
        return self._server

    def check_sync(self) -> dict[str, Any]:
        server = self._connect()
        return {
            "name": server.friendlyName,
            "machine_id": server.machineIdentifier,
            "version": server.version,
        }

    # -- accounts ----------------------------------------------------------
    def accounts_sync(self) -> list[PlexAccountInfo]:
        server = self._connect()
        out: list[PlexAccountInfo] = []
        for account in server.systemAccounts():
            account_id = getattr(account, "id", None)
            name = getattr(account, "name", None)
            if account_id is None:
                continue
            # id 0 is the "anonymous / local" pseudo-account; it has no name.
            if not name:
                continue
            out.append(PlexAccountInfo(int(account_id), str(name), getattr(account, "thumb", None)))
        return sorted(out, key=lambda a: a.id)

    # -- libraries ---------------------------------------------------------
    def libraries_sync(self) -> list[PlexLibraryInfo]:
        server = self._connect()
        out: list[PlexLibraryInfo] = []
        for section in server.library.sections():
            out.append(
                PlexLibraryInfo(
                    section_id=int(section.key),
                    title=str(section.title),
                    kind=str(getattr(section, "type", "") or ""),
                )
            )
        return out

    # -- enrichment --------------------------------------------------------
    def fetch_item_sync(self, rating_key: str) -> Any:
        server = self._connect()
        return server.fetchItem(f"/library/metadata/{rating_key}")

    def episode_context_sync(
        self, *, rating_key: str | None, show_rating_key: str | None
    ) -> EpisodeContext:
        """Resolve a scrobbled item to its show ids and season/episode numbers."""
        server = self._connect()
        item = server.fetchItem(f"/library/metadata/{rating_key}")
        item_type = str(getattr(item, "type", "") or "")

        if item_type == "movie":
            return EpisodeContext(
                show=show_ids_from_item(item),
                season=None,
                episode=None,
                media_kind="movie",
                show_rating_key=None,
                episode_title=getattr(item, "title", None),
            )

        season = getattr(item, "parentIndex", None)
        episode = getattr(item, "index", None)
        show_key = show_rating_key or getattr(item, "grandparentRatingKey", None)
        show_item = server.fetchItem(f"/library/metadata/{show_key}") if show_key else item
        return EpisodeContext(
            show=show_ids_from_item(show_item),
            season=int(season) if season is not None else None,
            episode=int(episode) if episode is not None else None,
            media_kind="episode",
            show_rating_key=str(show_key) if show_key else None,
            episode_title=getattr(item, "title", None),
        )

    # -- season counts -----------------------------------------------------
    def season_counts_sync(self, show_rating_key: str) -> list[SeasonInfo]:
        server = self._connect()
        show = server.fetchItem(f"/library/metadata/{show_rating_key}")
        by_season: dict[int, list[int]] = {}
        for episode in show.episodes():
            season = getattr(episode, "parentIndex", None)
            index = getattr(episode, "index", None)
            if season is None or index is None:
                continue
            by_season.setdefault(int(season), []).append(int(index))
        return [summarise_season(season, indexes) for season, indexes in sorted(by_season.items())]

    # -- history -----------------------------------------------------------
    def history_sync(self, *, account_id: int, mindate, maxresults: int = 500) -> list[Any]:
        server = self._connect()
        return server.history(maxresults=maxresults, mindate=mindate, accountID=account_id)


def summarise_season(season: int, indexes: list[int]) -> SeasonInfo:
    """Describe one season, and say whether its numbering has holes.

    A gap matters because absolute episode numbers are computed by adding up
    earlier seasons; if episodes are missing, that sum is wrong, and the match
    must be downgraded rather than silently believed.
    """
    unique = sorted(set(indexes))
    if not unique:
        return SeasonInfo(season, 0, None, None, True)
    lowest, highest = unique[0], unique[-1]
    contiguous = lowest == 1 and len(unique) == highest
    return SeasonInfo(season, len(unique), lowest, highest, contiguous)
