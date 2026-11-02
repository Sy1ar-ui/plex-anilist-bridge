"""Completing a series only once the user has rated it.

When the episode that would complete a title is watched, the pipeline writes
progress one short of the total and records a *pending completion* here, with
the local date that final episode was watched. The dashboard lists them. When
the user submits a rating (or chooses to complete without one), the completion
is written: COMPLETED, full progress, the score, and ``completedAt`` set to the
day the final episode was watched rather than the day of the rating.

The write goes through :class:`~app.sync.writegate.WriteGate` like every other,
so dry run applies. If AniList cannot be used, the rating is kept and the
completion is retried by the deferred-event job, as an event would be.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app import db, repo, settings_store
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListError,
    AniListUnavailable,
)
from app.anilist.rules import plan_rated_completion, score_to_raw
from app.sync.writegate import RESULT_DEFERRED, WriteGate

log = logging.getLogger(__name__)

AWAITING = "awaiting"
QUEUED = "queued"
DONE = "done"
DISMISSED = "dismissed"
OPEN = (AWAITING, QUEUED)


@dataclass
class Outcome:
    status: str          # done | queued | awaiting (after an error) | dismissed
    detail: str
    ok: bool


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
def record_pending(
    *,
    anilist_id: int,
    event_id: int | None,
    title: str | None,
    total_episodes: int,
    completed_on: date,
) -> bool:
    """Remember that a title is waiting for a rating. True if newly recorded.

    A title that is already waiting keeps its first record, so watching the
    finale again does not move the completion date.
    """
    with db.session() as conn:
        cursor = conn.execute(
            "INSERT INTO pending_completion (anilist_id, event_id, title, total_episodes,"
            " completed_on, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT (anilist_id) WHERE status IN ('awaiting', 'queued') DO NOTHING",
            (anilist_id, event_id, title, total_episodes, completed_on.isoformat(), AWAITING),
        )
        return bool(cursor.rowcount)


def get(pending_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM pending_completion WHERE id = ?", (pending_id,))
    return dict(row) if row else None


def open_for(anilist_id: int) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM pending_completion WHERE anilist_id = ? AND status IN (?, ?)",
        (anilist_id, *OPEN),
    )
    return dict(row) if row else None


def list_open() -> list[dict[str, Any]]:
    """Everything waiting on the user or on AniList, oldest finish first."""
    rows = db.query(
        "SELECT p.*, m.title_english, m.title_romaji, m.cover_url"
        " FROM pending_completion p"
        " LEFT JOIN anilist_media_cache m ON m.anilist_id = p.anilist_id"
        " WHERE p.status IN (?, ?)"
        " ORDER BY p.completed_on, p.id",
        OPEN,
    )
    items = []
    for row in rows:
        item = dict(row)
        item["display_title"] = (
            item.get("title") or item.get("title_english") or item.get("title_romaji")
            or f"AniList {item['anilist_id']}"
        )
        items.append(item)
    return items


def open_count() -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM pending_completion WHERE status = ?", (AWAITING,)
    )
    return int(row["n"]) if row else 0


def due_queued(*, limit: int = 25, ignore_schedule: bool = False) -> list[int]:
    sql = "SELECT id FROM pending_completion WHERE status = ?"
    if not ignore_schedule:
        sql += " AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now'))"
    rows = db.query(sql + " ORDER BY id LIMIT ?", (QUEUED, limit))
    return [int(r["id"]) for r in rows]


def queued_count() -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM pending_completion WHERE status = ?", (QUEUED,)
    )
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
# user actions
# --------------------------------------------------------------------------
def parse_score(raw: str | None) -> float:
    """A rating from the form: above 0, at most 10, at most one decimal."""
    try:
        score = float(str(raw or "").strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError("enter a rating from 1 to 10") from exc
    score_to_raw(score)  # range check
    return round(score, 1)


async def submit(
    pending_id: int,
    score: float | None,
    *,
    anilist: AniListClient | None = None,
    gate: WriteGate | None = None,
) -> Outcome:
    """Store the user's rating (None: complete without one) and try to write it."""
    item = get(pending_id)
    if item is None or item["status"] not in OPEN:
        return Outcome("missing", "that title is no longer waiting for a rating", False)

    db.execute(
        "UPDATE pending_completion SET status = ?, score = ?, last_error = NULL,"
        " next_attempt_at = NULL WHERE id = ?",
        (QUEUED, score, pending_id),
    )

    owned = anilist is None
    if anilist is None:
        try:
            anilist = AniListClient.from_db(max_wait=30.0, max_attempts=2)
        except AniListAuthError as exc:
            _schedule(pending_id, exc)
            return Outcome(
                QUEUED,
                f"Rating saved. AniList is not usable yet ({exc}); "
                "it will be sent automatically.",
                True,
            )
    try:
        return await apply(pending_id, anilist=anilist, gate=gate or WriteGate())
    finally:
        if owned:
            await anilist.__aexit__(None, None, None)


def dismiss(pending_id: int) -> Outcome:
    """Stop waiting. Nothing is written; the entry stays one episode short."""
    item = get(pending_id)
    if item is None or item["status"] not in OPEN:
        return Outcome("missing", "that title is no longer waiting for a rating", False)
    db.execute(
        "UPDATE pending_completion SET status = ?, resolved_at = datetime('now'),"
        " next_attempt_at = NULL WHERE id = ?",
        (DISMISSED, pending_id),
    )
    if item["event_id"]:
        repo.finalise_event(
            int(item["event_id"]),
            status="no_change",
            detail=(
                f"completion dismissed: left at {item['total_episodes'] - 1}/"
                f"{item['total_episodes']} on AniList"
            ),
        )
    return Outcome(DISMISSED, f"Stopped waiting for {item['title'] or 'that title'}.", True)


# --------------------------------------------------------------------------
# the write
# --------------------------------------------------------------------------
async def apply(pending_id: int, *, anilist: AniListClient, gate: WriteGate) -> Outcome:
    """Write one queued completion. Safe to repeat: the write is absolute."""
    item = get(pending_id)
    if item is None or item["status"] != QUEUED:
        return Outcome("missing", "nothing queued to write", False)

    anilist_id = int(item["anilist_id"])
    name = item["title"] or f"AniList {anilist_id}"
    score = item["score"]
    event_id = int(item["event_id"]) if item["event_id"] else None

    try:
        state, media = await anilist.media_with_entry(anilist_id)
    except (AniListUnavailable, AniListAuthError) as exc:
        _schedule(pending_id, exc)
        return Outcome(
            QUEUED,
            f"Rating saved. AniList is unavailable ({exc}); "
            f"{name} will be completed automatically when it is back.",
            True,
        )
    except AniListError as exc:
        return _failed(pending_id, f"could not read AniList: {exc}")

    repo.cache_media(media)
    plan = plan_rated_completion(
        state,
        total_episodes=int(item["total_episodes"]),
        completed_on=date.fromisoformat(item["completed_on"]),
        score=score,
        set_dates=bool(settings_store.get("set_dates", True)),
    )

    if plan.diff is None:
        _close(pending_id, event_id, "no_change", f"completion: {plan.reason}")
        return Outcome(DONE, f"{name}: {plan.reason}; nothing to write.", True)

    outcome = await gate.anilist_rated_completion(anilist, plan.diff, event_id=event_id)
    if outcome.result == RESULT_DEFERRED and outcome.error is not None:
        _schedule(pending_id, outcome.error)
        return Outcome(
            QUEUED,
            f"Rating saved. AniList is unavailable ({outcome.error}); "
            f"{name} will be completed automatically when it is back.",
            True,
        )
    if not outcome.ok:
        return _failed(pending_id, outcome.detail)

    status = "would_write" if outcome.dry_run else "written"
    _close(pending_id, event_id, status, f"completed after rating: {outcome.detail}")
    verb = "Dry run: would have completed" if outcome.dry_run else "Completed"
    rated = f" with {score:g}/10" if score is not None else " without a rating"
    return Outcome(DONE, f"{verb} {name}{rated}, finished {item['completed_on']}.", True)


async def retry_queued(
    *, anilist: AniListClient, gate: WriteGate, force: bool = False, limit: int = 25
) -> tuple[int, int]:
    """Retry due queued completions. Returns (attempted, completed)."""
    from app.anilist.client import health as anilist_health

    attempted = completed = 0
    for pending_id in due_queued(limit=limit, ignore_schedule=force):
        attempted += 1
        outcome = await apply(pending_id, anilist=anilist, gate=gate)
        if outcome.status == DONE:
            completed += 1
        elif outcome.status == QUEUED and anilist_health().blocked_reason():
            break
    return attempted, completed


def _schedule(pending_id: int, exc: Exception) -> None:
    from app.sync.pipeline import retry_delay

    item = get(pending_id) or {}
    attempts = int(item.get("attempts") or 0) + 1
    when = datetime.now(UTC) + retry_delay(attempts)
    retry_at = getattr(exc, "retry_at", None)
    if retry_at:
        when = max(when, datetime.fromtimestamp(retry_at, UTC))
    db.execute(
        "UPDATE pending_completion SET attempts = ?, next_attempt_at = ?, last_error = ?"
        " WHERE id = ?",
        (attempts, when.strftime("%Y-%m-%d %H:%M:%S"), str(exc), pending_id),
    )
    log.warning(
        "rated completion deferred", extra={"pending_id": pending_id, "error": str(exc)}
    )


def _failed(pending_id: int, message: str) -> Outcome:
    """Not a passing outage: hand it back to the user with the reason."""
    db.execute(
        "UPDATE pending_completion SET status = ?, next_attempt_at = NULL, last_error = ?"
        " WHERE id = ?",
        (AWAITING, message, pending_id),
    )
    return Outcome(AWAITING, f"Not completed: {message}", False)


def _close(pending_id: int, event_id: int | None, event_status: str, detail: str) -> None:
    db.execute(
        "UPDATE pending_completion SET status = ?, resolved_at = datetime('now'),"
        " next_attempt_at = NULL, last_error = NULL WHERE id = ?",
        (DONE, pending_id),
    )
    if event_id is not None:
        repo.finalise_event(event_id, status=event_status, detail=detail)
