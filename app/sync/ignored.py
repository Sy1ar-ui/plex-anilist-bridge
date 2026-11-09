"""Shows the user has dropped on purpose.

A correction says "this Plex show is that AniList title". This says the
opposite: "this is not anime, stop asking". It exists because a Plex server
holds more than anime -- a live-action series in a library that is being
watched, or an episode filed into the wrong folder -- and without it the only
remedy is dismissing the same show from the review queue every time another
episode is watched.

Dropping is always reversible and never writes to AniList or MyAnimeList: it
only stops the bridge considering the show.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app import db
from app.models import ShowIds

log = logging.getLogger(__name__)

SCOPE_SHOW = "plex_guid"
SCOPE_TVDB = "tvdb"


@dataclass(frozen=True)
class Ignored:
    id: int
    scope_type: str
    scope_key: str
    title: str | None
    created_at: str

    def describe(self) -> str:
        if self.scope_type == SCOPE_TVDB:
            return f"TVDB {self.scope_key}"
        return self.scope_key


def _keys(show: ShowIds) -> list[tuple[str, str]]:
    """Every identity this show could have been dropped under."""
    keys: list[tuple[str, str]] = []
    if show.plex_guid:
        keys.append((SCOPE_SHOW, show.plex_guid))
    if show.tvdb_id:
        keys.append((SCOPE_TVDB, str(show.tvdb_id)))
    return keys


def match(show: ShowIds) -> Ignored | None:
    """The drop rule covering this show, if there is one."""
    for scope_type, scope_key in _keys(show):
        row = db.query_one(
            "SELECT id, scope_type, scope_key, title, created_at FROM ignored_show"
            " WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        )
        if row:
            return Ignored(**dict(row))
    return None


def is_ignored(show: ShowIds) -> bool:
    return match(show) is not None


def add(*, scope_type: str, scope_key: str, title: str | None) -> bool:
    """Drop a show. Returns False if it was already dropped."""
    if scope_type not in (SCOPE_SHOW, SCOPE_TVDB) or not scope_key:
        raise ValueError(f"cannot drop a show scoped by {scope_type!r}")
    before = count()
    db.execute(
        "INSERT INTO ignored_show (scope_type, scope_key, title, created_at)"
        " VALUES (?, ?, ?, datetime('now'))"
        " ON CONFLICT (scope_type, scope_key) DO UPDATE SET title = COALESCE(excluded.title, ignored_show.title)",
        (scope_type, scope_key, title),
    )
    added = count() > before
    if added:
        log.info("show dropped", extra={"scope": scope_type, "key": scope_key, "title": title})
    return added


def remove(ignored_id: int) -> bool:
    row = db.query_one("SELECT id FROM ignored_show WHERE id = ?", (ignored_id,))
    if row is None:
        return False
    db.execute("DELETE FROM ignored_show WHERE id = ?", (ignored_id,))
    return True


def listing() -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.query(
            "SELECT id, scope_type, scope_key, title, created_at FROM ignored_show"
            " ORDER BY COALESCE(title, scope_key) COLLATE NOCASE"
        )
    ]


def count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM ignored_show")
    return int(row["n"]) if row else 0
