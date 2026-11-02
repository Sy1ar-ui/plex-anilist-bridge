"""PlexAniSync-Custom-Mappings loader.

A curated community dataset keyed on exact Plex GUIDs (``plex://show/<hex>``
and ``plex://movie/<hex>``), which is why it is consulted before the
id-and-offset dataset: a GUID hit needs no title matching and no arithmetic
guesswork, and the entries were checked by a human.

Shape of all three files:

    entries:
      - title: "Gintama"
        guid: plex://show/5d9c08273c3f87001f348b54
        seasons:
          - season: 10
            anilist-id: 100784
          - season: 10
            anilist-id: 101925
            start: 13

``start`` is the Plex episode at which the AniList entry begins, so it is
normalised here to ``ep_offset = start - 1``. Every layer then shares one
arithmetic, ``entry_episode = episode - ep_offset``. Upstream uses ``start: 0``
and even negative values to say that the AniList entry is *ahead* of the Plex
numbering (usually a special counted as an episode on AniList but not on TVDB);
a negative offset falls out of the same formula, so those are kept as-is.
"""
from __future__ import annotations

import logging
from typing import Any

import yaml

from app import db

log = logging.getLogger(__name__)

NAME = "community"
BASE_URL = "https://raw.githubusercontent.com/RickDB/PlexAniSync-Custom-Mappings/main"
FILES = ("series-tvdb.en.yaml", "series-tmdb.en.yaml", "movies-tmdb.en.yaml")

Row = tuple[str, int, int, int, str | None, str]


def url_for(filename: str) -> str:
    return f"{BASE_URL}/{filename}"


def parse_yaml(text: str, source: str) -> list[Row]:
    document = yaml.safe_load(text) or {}
    entries = document.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError(f"{source}: 'entries' is not a list")

    rows: list[Row] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        guid = (entry.get("guid") or "").strip()
        if not guid:
            # Title-only entries exist upstream for PlexAniSync's title matcher.
            # This project matches on ids, so a GUID-less entry is unusable.
            continue
        title = entry.get("title")
        for season_entry in entry.get("seasons") or []:
            if not isinstance(season_entry, dict):
                continue
            season = season_entry.get("season")
            anilist_id = season_entry.get("anilist-id")
            if not isinstance(season, int) or not isinstance(anilist_id, int):
                continue
            start = season_entry.get("start")
            start = start if isinstance(start, int) else 1
            rows.append((guid, season, start - 1, anilist_id, title, source))
    return rows


def load(rows: list[Row]) -> int:
    """Replace the cached dataset in one transaction.

    INSERT OR IGNORE collapses the overlap between the tvdb and tmdb series
    files, which list many of the same shows with identical mappings.
    """
    if not rows:
        raise ValueError("dataset parsed to zero rows; refusing to replace the cache")

    with db.session() as conn:
        conn.execute("DELETE FROM ds_community")
        conn.executemany(
            "INSERT OR IGNORE INTO ds_community"
            " (plex_guid, season, ep_offset, anilist_id, title, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM ds_community").fetchone()["n"]
    log.info("community dataset loaded", extra={"rows": int(count), "parsed": len(rows)})
    return int(count)


def row_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM ds_community")
    return int(row["n"]) if row else 0


def mappings_for_guid(plex_guid: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT season, ep_offset, anilist_id, title, source FROM ds_community"
        " WHERE plex_guid = ? ORDER BY season, ep_offset",
        (plex_guid,),
    )
    return [dict(r) for r in rows]
