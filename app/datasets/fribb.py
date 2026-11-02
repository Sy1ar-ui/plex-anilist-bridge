"""Fribb/anime-lists loader.

``anime-list-full.json`` merges AniDB <-> AniList <-> MAL with TVDB/TMDB ids,
seasons and episode offsets. Two shapes matter:

* ``season.tvdb`` present  -> a normal season, offset in ``episode_offset.tvdb``
* ``season`` absent on a TV row with a tvdb id -> the upstream XML used
  ``defaulttvdbseason="a"``, i.e. the show is absolute-numbered (One Piece).
  Fribb drops the non-numeric season, so the absence *is* the signal.

Parsing is pure and lives apart from the HTTP fetch, so it can be tested
against a fixture slice with no network.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app import db

log = logging.getLogger(__name__)

NAME = "fribb"
URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
CACHE_FILENAME = "anime-list-full.json"

MainRow = tuple[
    int, int | None, int | None, str | None, int | None, int | None, int,
    int | None, int | None, int,
]
ExtRow = tuple[int, str, str]


def _as_int(value: Any) -> int | None:
    """Coerce to int, or None. Non-numeric seasons such as "a" become None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_records(records: list[dict[str, Any]]) -> tuple[list[MainRow], list[ExtRow]]:
    main: list[MainRow] = []
    ext: list[ExtRow] = []

    for record in records:
        anidb_id = _as_int(record.get("anidb_id"))
        if anidb_id is None:
            continue

        season = record.get("season") or {}
        offset = record.get("episode_offset") or {}
        tmdb = record.get("themoviedb_id") or {}
        if not isinstance(season, dict):
            season = {}
        if not isinstance(offset, dict):
            offset = {}
        if not isinstance(tmdb, dict):
            tmdb = {}

        main.append(
            (
                anidb_id,
                _as_int(record.get("anilist_id")),
                _as_int(record.get("mal_id")),
                (record.get("type") or None),
                _as_int(record.get("tvdb_id")),
                _as_int(season.get("tvdb")),
                _as_int(offset.get("tvdb")) or 0,
                _as_int(tmdb.get("tv")),
                _as_int(season.get("tmdb")),
                _as_int(offset.get("tmdb")) or 0,
            )
        )

        for movie_id in _as_list(tmdb.get("movie")):
            as_int = _as_int(movie_id)
            if as_int is not None:
                ext.append((anidb_id, "tmdb_movie", str(as_int)))
        for imdb_id in _as_list(record.get("imdb_id")):
            if isinstance(imdb_id, str) and imdb_id.strip():
                ext.append((anidb_id, "imdb", imdb_id.strip()))

    return main, ext


def parse_bytes(raw: bytes) -> list[dict[str, Any]]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a JSON array at the top level")
    return data


def load(records: list[dict[str, Any]]) -> int:
    """Replace the cached dataset. One transaction, so readers never see a gap."""
    main, ext = parse_records(records)
    if not main:
        raise ValueError("dataset parsed to zero rows; refusing to replace the cache")

    with db.session() as conn:
        conn.execute("DELETE FROM ds_fribb_ext")
        conn.execute("DELETE FROM ds_fribb")
        conn.executemany(
            "INSERT OR REPLACE INTO ds_fribb (anidb_id, anilist_id, mal_id, kind,"
            " tvdb_id, tvdb_season, tvdb_offset, tmdb_tv_id, tmdb_season, tmdb_offset)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            main,
        )
        conn.executemany(
            "INSERT INTO ds_fribb_ext (anidb_id, kind, value) VALUES (?, ?, ?)",
            ext,
        )
    log.info("fribb dataset loaded", extra={"rows": len(main), "ext_rows": len(ext)})
    return len(main)


def row_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM ds_fribb")
    return int(row["n"]) if row else 0


def mal_id_for(anilist_id: int) -> int | None:
    row = db.query_one(
        "SELECT mal_id FROM ds_fribb WHERE anilist_id = ? AND mal_id IS NOT NULL",
        (anilist_id,),
    )
    return int(row["mal_id"]) if row else None
