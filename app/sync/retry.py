"""Catch-up for events deferred while AniList was down, throttling, or unauthorised.

The pipeline defers an event, rather than failing it, when AniList cannot be
used. This job runs the due ones through the same pipeline again. It is
careful not to make an outage worse:

* it does nothing while the circuit breaker says AniList is down, or while the
  token is marked broken (that needs the user, not a retry);
* it works in small batches, oldest first, through the shared rate limiter;
* it stops the moment AniList becomes unavailable again mid-batch, so the
  rest of the batch is not burned as failed attempts against a dead API.

Retrying is harmless by construction: every AniList write is absolute, and the
remote state is re-read before planning, so an event whose write did land
before a timeout becomes "no change" on retry.

Rated completions (:mod:`app.sync.completion`) that could not reach AniList
when the user submitted them are retried here too, after the events.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app import db, repo, settings_store, tokens
from app.anilist.client import AniListAuthError, AniListClient
from app.anilist.client import health as anilist_health
from app.plex import client as plex_client
from app.plex.client import PlexUnavailable
from app.sync import completion
from app.sync.pipeline import STATUS_DEFERRED, handle_watch_event
from app.sync.writegate import WriteGate

log = logging.getLogger(__name__)

BATCH_SIZE = 25
RETRYABLE = {STATUS_DEFERRED, "error"}

_running = asyncio.Lock()


@dataclass
class RetryReport:
    attempted: int = 0
    recovered: int = 0
    still_deferred: int = 0
    failed: int = 0
    completions_attempted: int = 0
    completions_done: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    detail: str = ""

    def summary(self) -> str:
        if not self.attempted and not self.completions_attempted:
            return self.detail or "nothing to retry"
        parts = []
        if self.attempted:
            text = (
                f"retried {self.attempted} deferred event(s): {self.recovered} went "
                f"through, {self.still_deferred} still waiting"
            )
            if self.failed:
                text += f", {self.failed} failed"
            parts.append(text)
        if self.completions_attempted:
            parts.append(
                f"{self.completions_done} of {self.completions_attempted} rated "
                "completion(s) written"
            )
        text = "; ".join(parts)
        if self.detail:
            text += f" ({self.detail})"
        return text


def is_running() -> bool:
    return _running.locked()


def blocked_reason() -> str | None:
    """Why the retry job would not run right now, if it would not."""
    if not settings_store.get("sync_enabled", True):
        return "syncing is turned off in settings"
    record = tokens.get(tokens.ANILIST)
    if not record.access_token:
        return "AniList is not connected"
    if record.status == tokens.STATUS_BROKEN:
        return "the AniList token needs re-authorising"
    return anilist_health().blocked_reason()


async def retry_deferred(*, force: bool = False, limit: int = BATCH_SIZE) -> RetryReport:
    """Retry deferred events that are due. ``force`` ignores per-event backoff.

    ``force`` does not override the circuit breaker: if AniList is known to be
    down, hammering it from a button press does not help. "Check AniList now"
    on the dashboard is the way to test whether it is back.
    """
    report = RetryReport()
    if _running.locked():
        report.detail = "a retry run is already in progress"
        return report

    async with _running:
        reason = blocked_reason()
        if reason:
            report.detail = f"not retrying: {reason}"
            _record(report)
            return report

        event_ids = repo.due_deferred_events(limit=limit, ignore_schedule=force)
        completion_ids = completion.due_queued(limit=limit, ignore_schedule=force)
        if not event_ids and not completion_ids:
            report.detail = "no deferred events are due"
            _record(report)
            return report

        try:
            anilist = AniListClient.from_db()
        except AniListAuthError as exc:
            report.detail = f"not retrying: {exc}"
            _record(report)
            return report

        plex = None
        try:
            plex = plex_client.PlexClient.from_db()
        except PlexUnavailable:
            plex = None  # only needed to refresh season counts; not essential

        gate = WriteGate()
        try:
            for event_id in event_ids:
                result = await _run_one(event_id, plex=plex, anilist=anilist, gate=gate)
                if result is None:
                    report.failed += 1
                    continue
                report.attempted += 1
                report.outcomes[result] = report.outcomes.get(result, 0) + 1
                if result == STATUS_DEFERRED:
                    report.still_deferred += 1
                    stop = blocked_reason()
                    if stop:
                        report.detail = f"stopped early: {stop}"
                        break
                elif result == "error":
                    report.failed += 1
                else:
                    report.recovered += 1

            if completion_ids and not blocked_reason():
                attempted, done = await completion.retry_queued(
                    anilist=anilist, gate=gate, force=force, limit=limit
                )
                report.completions_attempted = attempted
                report.completions_done = done
        finally:
            await anilist.__aexit__(None, None, None)

    _record(report)
    log.info("deferred retry finished", extra={"summary": report.summary()})
    return report


async def retry_event(event_id: int) -> tuple[str | None, str]:
    """Retry one event now, from the event page. Works for errors too.

    Returns ``(status, detail)``; status is None if the event cannot be retried.
    """
    current = repo.event_status(event_id)
    if current is None:
        return None, "no such event"
    if current not in RETRYABLE:
        return None, f"only deferred or failed events can be retried (this one is {current})"

    try:
        # A deliberate click is allowed through an open circuit: it doubles as
        # the probe that tells the breaker AniList is back.
        anilist = AniListClient.from_db(max_wait=30.0, max_attempts=2, respect_circuit=False)
    except AniListAuthError as exc:
        return None, str(exc)

    plex = None
    try:
        plex = plex_client.PlexClient.from_db()
    except PlexUnavailable:
        plex = None

    try:
        status = await _run_one(event_id, plex=plex, anilist=anilist, gate=WriteGate())
    finally:
        await anilist.__aexit__(None, None, None)
    if status is None:
        return None, "the original event details could not be rebuilt"
    row = db.query_one("SELECT detail FROM event WHERE id = ?", (event_id,))
    return status, (row["detail"] if row else "")


async def _run_one(event_id: int, *, plex, anilist, gate) -> str | None:
    event = repo.event_input(event_id)
    if event is None:
        repo.finalise_event(
            event_id,
            status="error",
            detail="cannot retry: the original event details were not recorded",
        )
        return None
    try:
        result = await handle_watch_event(
            event, plex=plex, anilist=anilist, gate=gate, event_id=event_id
        )
    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the batch
        log.exception("retry of event failed", extra={"event_id": event_id})
        repo.finalise_event(event_id, status="error", detail=f"retry failed: {exc}")
        return "error"
    return result.status


def _record(report: RetryReport) -> None:
    db.execute(
        "INSERT INTO watermark (name, value) VALUES ('last_anilist_retry', datetime('now'))"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value"
    )
    db.execute(
        "INSERT INTO watermark (name, value) VALUES ('last_anilist_retry_summary', ?)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (report.summary(),),
    )
