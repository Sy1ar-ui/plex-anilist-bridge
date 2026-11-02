"""Persistence for events, write attempts, shows and overrides."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app import db
from app.mapping.seasons import SeasonInfo, save_season_counts
from app.models import MatchResult, ShowIds, WatchEvent

log = logging.getLogger(__name__)

# Two plays of the same episode inside this many seconds are treated as one
# event. The key is (rating key, account, time bucket) and deliberately leaves
# out the source, so a webhook and the reconcile sweep for the same play
# collapse into one event rather than two. This is for a tidy log and to avoid
# re-applying work; it is not what prevents double-counting -- absolute writes
# do that, which is why a missed collapse is harmless.

DEDUPE_BUCKET_SECONDS = 300
COLLAPSE_WINDOW_SECONDS = 900


@dataclass
class StoredEvent:
    id: int
    is_new: bool


def dedupe_key(
    *, rating_key: str | None, account_id: int | None, moment: datetime
) -> str:
    bucket = int(moment.timestamp() // DEDUPE_BUCKET_SECONDS)
    return f"{rating_key or 'unknown'}|{account_id if account_id is not None else '-'}|{bucket}"


def recent_event_exists(
    *, rating_key: str | None, account_id: int | None, moment: datetime
) -> bool:
    """Has this play already been logged, from either source?"""
    if not rating_key:
        return False
    row = db.query_one(
        "SELECT id FROM event"
        " WHERE plex_rating_key = ?"
        "   AND (plex_account_id IS ? OR plex_account_id = ?)"
        "   AND ABS(strftime('%s', COALESCE(viewed_at, received_at)) - ?) < ?"
        " LIMIT 1",
        (
            rating_key,
            account_id,
            account_id,
            int(moment.timestamp()),
            COLLAPSE_WINDOW_SECONDS,
        ),
    )
    return row is not None


def record_event(
    event: WatchEvent,
    *,
    status: str,
    match: MatchResult | None,
    detail: str,
    entry_episode: int | None = None,
    anilist_id: int | None = None,
) -> StoredEvent:
    """Insert the event, or return the existing row if it is a replay."""
    moment = event.viewed_at or event.received_at
    key = dedupe_key(
        rating_key=event.rating_key, account_id=event.plex_account_id, moment=moment
    )

    with db.session() as conn:
        cursor = conn.execute(
            "INSERT INTO event (source, received_at, viewed_at, plex_account_id,"
            " plex_rating_key, show_plex_guid, show_title, season, episode, media_kind,"
            " status, confidence, anilist_id, entry_episode, detail, trace_json, dedupe_key,"
            " input_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (dedupe_key) DO NOTHING",
            (
                event.source,
                event.received_at.isoformat(),
                event.viewed_at.isoformat() if event.viewed_at else None,
                event.plex_account_id,
                event.rating_key,
                event.show.plex_guid,
                event.show.title,
                event.season,
                event.episode,
                event.media_kind,
                status,
                match.confidence if match else None,
                anilist_id,
                entry_episode,
                detail,
                match.trace_json() if match else None,
                key,
                event.to_json(),
            ),
        )
        if cursor.rowcount:
            return StoredEvent(int(cursor.lastrowid), True)
        existing = conn.execute(
            "SELECT id FROM event WHERE dedupe_key = ?", (key,)
        ).fetchone()
        return StoredEvent(int(existing["id"]), False)


def update_event_status(
    event_id: int, *, status: str, detail: str, entry_episode: int | None = None
) -> None:
    db.execute(
        "UPDATE event SET status = ?, detail = ?,"
        " entry_episode = COALESCE(?, entry_episode) WHERE id = ?",
        (status, detail, entry_episode, event_id),
    )


def log_write(
    *,
    event_id: int | None,
    target: str,
    dry_run: bool,
    operation: str,
    request: Any,
    before: Any,
    after: Any,
    result: str,
    error: str | None = None,
) -> int:
    import json

    return db.execute(
        "INSERT INTO write_log (event_id, target, dry_run, operation, request_json,"
        " before_json, after_json, result, error, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            event_id,
            target,
            1 if dry_run else 0,
            operation,
            json.dumps(request, default=str) if request is not None else None,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            result,
            error,
        ),
    )


# --------------------------------------------------------------------------
# shows
# --------------------------------------------------------------------------
def upsert_show(ids: ShowIds, *, rating_key: str | None, section_id: int | None) -> None:
    if not ids.plex_guid:
        return
    db.execute(
        "INSERT INTO show (plex_guid, rating_key, section_id, title, year, tvdb_id,"
        " tmdb_id, imdb_id, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT (plex_guid) DO UPDATE SET"
        "   rating_key = COALESCE(excluded.rating_key, show.rating_key),"
        "   section_id = COALESCE(excluded.section_id, show.section_id),"
        "   title = COALESCE(excluded.title, show.title),"
        "   year = COALESCE(excluded.year, show.year),"
        "   tvdb_id = COALESCE(excluded.tvdb_id, show.tvdb_id),"
        "   tmdb_id = COALESCE(excluded.tmdb_id, show.tmdb_id),"
        "   imdb_id = COALESCE(excluded.imdb_id, show.imdb_id),"
        "   last_seen = excluded.last_seen",
        (
            ids.plex_guid,
            rating_key,
            section_id,
            ids.title,
            ids.year,
            ids.tvdb_id,
            ids.tmdb_id,
            ids.imdb_id,
        ),
    )


def season_counts_are_stale(plex_guid: str, *, max_age_hours: int = 24) -> bool:
    row = db.query_one(
        "SELECT MAX(refreshed_at) AS newest FROM show_season WHERE plex_guid = ?",
        (plex_guid,),
    )
    if not row or not row["newest"]:
        return True
    age = db.query_one(
        "SELECT (julianday('now') - julianday(?)) * 24 AS hours", (row["newest"],)
    )
    return bool(age and age["hours"] is not None and age["hours"] > max_age_hours)


def store_season_counts(plex_guid: str, infos: list[SeasonInfo]) -> None:
    save_season_counts(plex_guid, infos)


def cache_media(media: dict[str, Any]) -> None:
    """Remember an AniList title so later matches can sanity-check episode counts."""
    if not media or not media.get("id"):
        return
    title = media.get("title") or {}
    cover = media.get("coverImage") or {}
    db.execute(
        "INSERT INTO anilist_media_cache (anilist_id, title_romaji, title_english,"
        " format, season_year, episodes, cover_url, id_mal, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT (anilist_id) DO UPDATE SET"
        "   title_romaji = excluded.title_romaji,"
        "   title_english = excluded.title_english,"
        "   format = excluded.format,"
        "   season_year = excluded.season_year,"
        "   episodes = excluded.episodes,"
        "   cover_url = excluded.cover_url,"
        "   id_mal = excluded.id_mal,"
        "   fetched_at = excluded.fetched_at",
        (
            int(media["id"]),
            title.get("romaji"),
            title.get("english"),
            media.get("format"),
            media.get("seasonYear"),
            media.get("episodes"),
            cover.get("medium"),
            media.get("idMal"),
        ),
    )


# --------------------------------------------------------------------------
# activity log reads
# --------------------------------------------------------------------------
def recent_events(
    *, limit: int = 50, offset: int = 0, status: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append("show_title LIKE ?")
        params.append(f"%{search}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])
    rows = db.query(
        "SELECT * FROM event" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", params
    )
    return [dict(r) for r in rows]


def event_count(*, status: str | None = None) -> int:
    if status:
        row = db.query_one("SELECT COUNT(*) AS n FROM event WHERE status = ?", (status,))
    else:
        row = db.query_one("SELECT COUNT(*) AS n FROM event")
    return int(row["n"]) if row else 0


def pending_review_count() -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM event WHERE status IN ('held', 'unmatched')"
    )
    return int(row["n"]) if row else 0


def writes_for_event(event_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM write_log WHERE event_id = ? ORDER BY id", (event_id,)
    )
    return [dict(r) for r in rows]


def last_event_at(source: str | None = None) -> str | None:
    if source:
        row = db.query_one(
            "SELECT MAX(received_at) AS newest FROM event WHERE source = ?", (source,)
        )
    else:
        row = db.query_one("SELECT MAX(received_at) AS newest FROM event")
    return row["newest"] if row else None


def now() -> datetime:
    return datetime.now(UTC)


def event_status(event_id: int) -> str | None:
    row = db.query_one("SELECT status FROM event WHERE id = ?", (event_id,))
    return row["status"] if row else None


def finalise_event(
    event_id: int,
    *,
    status: str,
    detail: str,
    match: MatchResult | None = None,
    anilist_id: int | None = None,
    entry_episode: int | None = None,
) -> None:
    """Write the outcome of processing onto the event row.

    Clears any scheduled retry: an event is only due again if
    :func:`schedule_retry` is called after this.
    """
    db.execute(
        "UPDATE event SET status = ?, detail = ?, next_attempt_at = NULL,"
        " confidence = COALESCE(?, confidence),"
        " trace_json = COALESCE(?, trace_json),"
        " anilist_id = COALESCE(?, anilist_id),"
        " entry_episode = COALESCE(?, entry_episode)"
        " WHERE id = ?",
        (
            status,
            detail,
            match.confidence if match else None,
            match.trace_json() if match else None,
            anilist_id,
            entry_episode,
            event_id,
        ),
    )


# --------------------------------------------------------------------------
# deferred events (AniList unavailable) and retries
# --------------------------------------------------------------------------
STATUS_DEFERRED = "deferred"


def _sql_time(moment: datetime) -> str:
    """The format SQLite's datetime('now') uses, so the two compare as text."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def event_attempts(event_id: int) -> int:
    row = db.query_one("SELECT attempts FROM event WHERE id = ?", (event_id,))
    return int(row["attempts"] or 0) if row else 0


def record_attempts(event_id: int, attempts: int) -> None:
    db.execute("UPDATE event SET attempts = ? WHERE id = ?", (attempts, event_id))


def schedule_retry(event_id: int, *, attempts: int, next_attempt_at: datetime) -> None:
    db.execute(
        "UPDATE event SET attempts = ?, next_attempt_at = ? WHERE id = ?",
        (attempts, _sql_time(next_attempt_at), event_id),
    )


def due_deferred_events(*, limit: int = 25, ignore_schedule: bool = False) -> list[int]:
    """Deferred events whose retry time has come, oldest first."""
    if ignore_schedule:
        rows = db.query(
            "SELECT id FROM event WHERE status = ? ORDER BY id LIMIT ?",
            (STATUS_DEFERRED, limit),
        )
    else:
        rows = db.query(
            "SELECT id FROM event WHERE status = ?"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))"
            " ORDER BY id LIMIT ?",
            (STATUS_DEFERRED, limit),
        )
    return [int(r["id"]) for r in rows]


def deferred_count() -> int:
    return event_count(status=STATUS_DEFERRED)


def next_deferred_attempt() -> str | None:
    row = db.query_one(
        "SELECT MIN(next_attempt_at) AS soonest FROM event WHERE status = ?",
        (STATUS_DEFERRED,),
    )
    return row["soonest"] if row else None


def event_input(event_id: int) -> WatchEvent | None:
    """Rebuild the WatchEvent an event row was created from, for a retry.

    Rows written since retries existed carry the exact input. Older rows are
    rebuilt from their columns plus the show table, which holds the external
    ids the enrichment step found.
    """
    row = db.query_one("SELECT * FROM event WHERE id = ?", (event_id,))
    if row is None:
        return None
    if row["input_json"]:
        try:
            return WatchEvent.from_json(row["input_json"])
        except (ValueError, TypeError, KeyError):
            log.warning("stored event input unreadable; rebuilding", extra={"event_id": event_id})

    show_row = None
    if row["show_plex_guid"]:
        show_row = db.query_one(
            "SELECT * FROM show WHERE plex_guid = ?", (row["show_plex_guid"],)
        )
    show = ShowIds(
        plex_guid=row["show_plex_guid"],
        tvdb_id=show_row["tvdb_id"] if show_row else None,
        tmdb_id=show_row["tmdb_id"] if show_row else None,
        imdb_id=show_row["imdb_id"] if show_row else None,
        title=row["show_title"] or (show_row["title"] if show_row else None),
        year=show_row["year"] if show_row else None,
    )

    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    return WatchEvent(
        source=row["source"],
        received_at=parse(row["received_at"]) or now(),
        viewed_at=parse(row["viewed_at"]),
        show=show,
        media_kind=row["media_kind"] or "episode",
        season=row["season"],
        episode=row["episode"],
        plex_account_id=row["plex_account_id"],
        rating_key=row["plex_rating_key"],
        show_rating_key=show_row["rating_key"] if show_row else None,
    )


def status_counts(*, since_hours: int | None = None) -> dict[str, int]:
    """How many events ended in each status, optionally over a recent window."""
    if since_hours:
        rows = db.query(
            "SELECT status, COUNT(*) AS n FROM event"
            " WHERE received_at >= ? GROUP BY status",
            (_iso_hours_ago(since_hours),),
        )
    else:
        rows = db.query("SELECT status, COUNT(*) AS n FROM event GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


def _iso_hours_ago(hours: int) -> str:
    from datetime import timedelta

    return (now() - timedelta(hours=hours)).isoformat()
