"""The safety net: a periodic sweep of the configured account's Plex history.

Webhooks are the primary trigger, but they are fire-and-forget -- anything
watched while the container was down or restarting is simply lost. This sweep
re-reads Plex's own watch history for the configured account only and feeds
anything it has not already seen through the same pipeline.

It cannot double-count: the pipeline's dedupe collapses a play the webhook
already handled, and every AniList write is absolute rather than an increment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app import db, repo, settings_store
from app.anilist.client import AniListClient, AniListAuthError
from app.models import WatchEvent
from app.plex import client as plex_client
from app.plex.client import PlexClient, PlexUnavailable
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    considered: int = 0
    processed: int = 0
    skipped_already_seen: int = 0
    skipped_other_library: int = 0
    errors: list[str] = field(default_factory=list)
    detail: str = ""

    def summary(self) -> str:
        return (
            f"{self.considered} history entries, {self.processed} processed, "
            f"{self.skipped_already_seen} already seen, "
            f"{self.skipped_other_library} outside the enabled libraries"
            + (f", {len(self.errors)} errors" if self.errors else "")
        )


def _viewed_at(item) -> datetime:
    raw = getattr(item, "viewedAt", None)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return datetime.now(UTC)


async def run_reconcile(*, lookback_hours: int | None = None) -> ReconcileReport:
    report = ReconcileReport()
    settings = settings_store.all_settings()

    if not settings.get("sync_enabled", True):
        report.detail = "syncing is turned off in settings"
        return report

    account_id = plex_client.configured_account_id()
    if account_id is None:
        report.detail = "no Plex account has been chosen yet, so nothing can be filtered safely"
        return report

    try:
        plex = PlexClient.from_db()
    except PlexUnavailable as exc:
        report.detail = f"Plex is not configured: {exc}"
        return report

    hours = lookback_hours or int(settings.get("reconcile_lookback_hours", 72))
    mindate = datetime.now(UTC) - timedelta(hours=hours)
    enabled = plex_client.enabled_section_ids()

    try:
        history = await plex_client.run(
            plex.history_sync, account_id=account_id, mindate=mindate
        )
    except Exception as exc:  # noqa: BLE001 - reported to the UI as text
        report.detail = f"could not read Plex history: {exc}"
        report.errors.append(str(exc))
        return report

    anilist: AniListClient | None = None
    try:
        anilist = AniListClient.from_db()
    except AniListAuthError:
        anilist = None

    gate = WriteGate()

    try:
        for item in history:
            report.considered += 1

            # The history query is already filtered by account, but this is the
            # one guarantee worth re-checking rather than trusting.
            item_account = getattr(item, "accountID", None)
            if item_account is not None and int(item_account) != account_id:
                continue

            rating_key = getattr(item, "ratingKey", None)
            if rating_key is None:
                continue
            rating_key = str(rating_key)

            section_id = getattr(item, "librarySectionID", None)
            if enabled and section_id is not None and int(section_id) not in enabled:
                report.skipped_other_library += 1
                continue

            viewed = _viewed_at(item)
            if repo.recent_event_exists(
                rating_key=rating_key, account_id=account_id, moment=viewed
            ):
                report.skipped_already_seen += 1
                continue

            try:
                context = await plex_client.run(
                    plex.episode_context_sync,
                    rating_key=rating_key,
                    show_rating_key=getattr(item, "grandparentRatingKey", None),
                )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{rating_key}: {exc}")
                continue

            event = WatchEvent(
                source="reconcile",
                received_at=datetime.now(UTC),
                viewed_at=viewed,
                show=context.show,
                media_kind=context.media_kind,
                season=context.season,
                episode=context.episode,
                plex_account_id=account_id,
                rating_key=rating_key,
                show_rating_key=context.show_rating_key,
            )

            result = await handle_watch_event(
                event, plex=plex, anilist=anilist, gate=gate
            )
            report.processed += 1
            log.info(
                "reconcile event",
                extra={
                    "event_id": result.event_id,
                    "status": result.status,
                    "label": event.label(),
                },
            )
    finally:
        if anilist is not None:
            await anilist.__aexit__(None, None, None)

    report.detail = report.summary()
    db.execute(
        "INSERT INTO watermark (name, value) VALUES ('last_reconcile', datetime('now'))"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value"
    )
    db.execute(
        "INSERT INTO watermark (name, value) VALUES ('last_reconcile_summary', ?)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (report.detail,),
    )
    log.info("reconcile finished", extra={"summary": report.detail})
    return report


def last_run_at() -> str | None:
    row = db.query_one("SELECT value FROM watermark WHERE name = 'last_reconcile'")
    return row["value"] if row else None
