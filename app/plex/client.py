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

from app import db, settings_store
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


def stored_machine_id() -> str | None:
    """The connected server's machine identifier, if it was recorded."""
    row = db.query_one("SELECT machine_id FROM plex_server WHERE id = 1")
    return row["machine_id"] if row and row["machine_id"] else None


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
    # A confirmed webhook id belonged to the previous choice.
    settings_store.set_many({WEBHOOK_ACCOUNT_ALIAS: None, WEBHOOK_ACCOUNT_CANDIDATE: None})


# --------------------------------------------------------------------------
# the webhook account id
#
# Plex documents a server owner's webhook Account.id as always 1, the owner's
# local account id. Real servers have been seen sending the owner's plex.tv
# account id instead, so every one of the owner's events was dropped as
# another account's. Two ways in, both exact ids, never a guess:
#
# * The owner's plex.tv id, recorded at the Plex sign-in in setup, counts
#   automatically -- but only while the owner (local id 1) is the chosen
#   account and the signed-in account owns the server.
# * Otherwise the bridge notices an event that is plainly the chosen
#   account's -- by name, or by Plex's own owner flags -- remembers the id it
#   carries as a *candidate*, and asks. Only once the user confirms it does
#   the account filter accept that id too (the *alias*).
# --------------------------------------------------------------------------
SIGNED_IN_AS = "plex_signed_in_as"
WEBHOOK_ACCOUNT_ALIAS = "plex_webhook_account_alias"
WEBHOOK_ACCOUNT_CANDIDATE = "plex_webhook_account_candidate"


def configured_account_name() -> str | None:
    row = db.query_one("SELECT name FROM plex_account LIMIT 1")
    return row["name"] if row and row["name"] else None


def own_names() -> set[str]:
    """Every name the chosen account goes by, lower-cased.

    The stored name, plus the username and display name from the Plex
    sign-in when that sign-in was the server owner and the owner is chosen.
    A name that is only digits is an id standing in for a name, so it is left
    out.
    """
    names = {configured_account_name() or ""}
    hint = settings_store.get(SIGNED_IN_AS, None) or {}
    if isinstance(hint, dict) and hint.get("owned") and configured_account_id() == 1:
        names |= {str(hint.get("username") or ""), str(hint.get("title") or "")}
    return {n.strip().lower() for n in names if n.strip() and not n.strip().isdigit()}


def owner_plex_tv_id() -> int | None:
    """The owner's plex.tv account id, when the owner is the chosen account."""
    hint = settings_store.get(SIGNED_IN_AS, None) or {}
    if not isinstance(hint, dict) or not hint.get("owned") or configured_account_id() != 1:
        return None
    value = hint.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 1 else None


def webhook_account_ids() -> set[int]:
    """Ids besides the chosen account's own that its webhooks may carry."""
    return {i for i in (owner_plex_tv_id(), webhook_account_alias()) if i is not None}


def webhook_account_alias() -> int | None:
    value = settings_store.get(WEBHOOK_ACCOUNT_ALIAS, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def set_webhook_account_alias(account_id: int) -> None:
    settings_store.set_many({WEBHOOK_ACCOUNT_ALIAS: int(account_id), WEBHOOK_ACCOUNT_CANDIDATE: None})


def webhook_account_candidate() -> dict[str, Any] | None:
    value = settings_store.get(WEBHOOK_ACCOUNT_CANDIDATE, None)
    if isinstance(value, dict) and isinstance(value.get("id"), int):
        return value
    return None


def note_webhook_account_candidate(account_id: int, reasons: list[str]) -> None:
    """Remember an id seen on an event that looks like the chosen account's."""
    current = webhook_account_candidate()
    if current and current["id"] == account_id:
        seen = int(current.get("seen") or 0) + 1
        reasons = sorted(set(reasons) | set(current.get("reasons") or []))
    else:
        seen = 1
    settings_store.set(WEBHOOK_ACCOUNT_CANDIDATE, {"id": account_id, "seen": seen, "reasons": sorted(reasons)})


def save_libraries(libraries: list[PlexLibraryInfo], chosen: set[int]) -> None:
    """Record every library the server has, enabling the chosen ones."""
    with db.session() as conn:
        for lib in libraries:
            conn.execute(
                "INSERT INTO library (section_id, title, kind, enabled)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (section_id) DO UPDATE SET title = excluded.title,"
                "   kind = excluded.kind, enabled = excluded.enabled",
                (lib.section_id, lib.title, lib.kind, 1 if lib.section_id in chosen else 0),
            )


async def save_libraries_from_server(chosen: set[int]) -> None:
    server = stored_server()
    if not server:
        return
    try:
        libraries = await run(PlexClient(*server).libraries_sync)
    except Exception:  # noqa: BLE001 - nothing to save if the server cannot be read
        libraries = []
    save_libraries(libraries, chosen)


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
        folder=folder_name(getattr(item, "locations", None)),
    )


def folder_name(locations: Any) -> str | None:
    """The last component of a show's first folder, whatever the OS or mount."""
    if not locations:
        return None
    first = str(next(iter(locations))).replace("\\", "/").rstrip("/")
    name = first.rsplit("/", 1)[-1].strip()
    return name or None


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
