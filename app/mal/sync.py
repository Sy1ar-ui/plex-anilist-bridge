"""The AniList -> MyAnimeList poller.

Every few minutes (``mal_poll_interval_minutes``) it asks AniList for the
user's list entries changed since the last run, newest first, and stops at the
watermark. It then mirrors them oldest first, moving the watermark past each
entry as it is handled, so a run cut short by a MAL outage resumes exactly
where it stopped. Writes are absolute, so handling an entry twice is harmless.

The first run only sets the watermark to "now": existing differences are the
job of the full reconcile preview (:mod:`app.mal.reconcile`), where the user
sees them before anything is written.

Dry run keeps its own watermark. Turning dry run off later therefore still
mirrors every change made while it was on, instead of treating them as done.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app import db, settings_store, tokens
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListError,
    AniListUnavailable,
)
from app.mal.client import MALAuthError, MALClient, MALError, MALUnavailable
from app.mal.rules import MalSettings, plan_mal_write
from app.sync.writegate import RESULT_DEFERRED, WriteGate

log = logging.getLogger(__name__)

PER_PAGE = 50
MAX_PAGES = 10   # 500 changes per run; more are picked up by the next run

_running = asyncio.Lock()


@dataclass
class PollReport:
    seen: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    detail: str = ""

    def count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def summary(self) -> str:
        if not self.seen:
            return self.detail or "no AniList changes since the last run"
        parts = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(self.outcomes.items()))
        text = f"{self.seen} AniList change(s): {parts}"
        return f"{text} ({self.detail})" if self.detail else text


def mal_settings() -> MalSettings:
    return MalSettings(
        sync_score=bool(settings_store.get("mal_sync_score", True)),
        sync_dates=bool(settings_store.get("mal_sync_dates", False)),
    )


def watermark_key(dry_run: bool) -> str:
    return "mal_poll_watermark_dry" if dry_run else "mal_poll_watermark"


def get_watermark(dry_run: bool) -> int | None:
    row = db.query_one("SELECT value FROM watermark WHERE name = ?", (watermark_key(dry_run),))
    try:
        return int(row["value"]) if row and row["value"] else None
    except (TypeError, ValueError):
        return None


def set_watermark(dry_run: bool, value: int) -> None:
    _set(watermark_key(dry_run), str(int(value)))


def _set(name: str, value: str) -> None:
    db.execute(
        "INSERT INTO watermark (name, value) VALUES (?, ?)"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
        (name, value),
    )


def log_item(*, source: str, entry: dict[str, Any] | None, mal_id: int | None, outcome: str, detail: str,
             title: str | None = None, anilist_id: int | None = None) -> None:
    media = (entry or {}).get("media") or {}
    names = media.get("title") or {}
    db.execute(
        "INSERT INTO mal_sync_log (at, source, anilist_id, mal_id, title, outcome, detail)"
        " VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
        (source, media.get("id") or anilist_id, mal_id,
         title or names.get("english") or names.get("romaji"), outcome, detail),
    )


def recent_log(limit: int = 40) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query("SELECT * FROM mal_sync_log ORDER BY id DESC LIMIT ?", (limit,))]


def blocked_reason() -> str | None:
    if not settings_store.get("mal_sync_enabled", False):
        return "MyAnimeList sync is turned off in settings"
    anilist = tokens.get(tokens.ANILIST)
    if not anilist.present or anilist.account_id is None:
        return "AniList is not connected"
    mal = tokens.get(tokens.MAL)
    if not mal.present:
        return "MyAnimeList is not connected"
    if mal.status == tokens.STATUS_BROKEN:
        return "the MyAnimeList token needs re-authorising"
    return None


def is_running() -> bool:
    return _running.locked()


async def poll(
    *,
    anilist: AniListClient | None = None,
    mal: MALClient | None = None,
    gate: WriteGate | None = None,
) -> PollReport:
    report = PollReport()
    if _running.locked():
        report.detail = "a run is already in progress"
        return report
    async with _running:
        reason = blocked_reason()
        if reason:
            report.detail = reason
            _record(report)
            return report
        gate = gate or WriteGate()
        mark = get_watermark(gate.dry_run)
        if mark is None:
            set_watermark(gate.dry_run, int(time.time()))
            report.detail = ("started mirroring from now; run the full reconcile to bring "
                             "existing entries across")
            _record(report)
            return report

        owned_anilist, owned_mal = anilist is None, mal is None
        try:
            anilist = anilist or AniListClient.from_db()
            mal = mal or await MALClient.from_db()
            await _mirror_changes(report, anilist=anilist, mal=mal, gate=gate, mark=mark)
        except (AniListAuthError, MALAuthError) as exc:
            report.detail = f"stopped: {exc}"
        finally:
            if owned_anilist and anilist is not None:
                await anilist.__aexit__(None, None, None)
            if owned_mal and mal is not None:
                await mal.__aexit__(None, None, None)
    _record(report)
    log.info("MAL poll finished", extra={"summary": report.summary()})
    return report


async def _mirror_changes(report: PollReport, *, anilist, mal, gate: WriteGate, mark: int) -> None:
    user_id = int(tokens.get(tokens.ANILIST).account_id)
    changed: list[dict[str, Any]] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            entries, more = await anilist.recent_list_changes(user_id, page=page, per_page=PER_PAGE)
            reached = False
            for entry in entries:
                if int(entry.get("updatedAt") or 0) <= mark:
                    reached = True
                    break
                changed.append(entry)
            if reached or not more:
                break
    except (AniListUnavailable, AniListError) as exc:
        report.detail = f"AniList could not be read: {exc}"
        return

    settings = mal_settings()

    def advance(updated: int) -> None:
        nonlocal mark
        mark = max(mark, updated)
        set_watermark(gate.dry_run, mark)

    for entry in reversed(changed):   # oldest first, so the watermark only moves forward
        report.seen += 1
        updated = int(entry.get("updatedAt") or 0)
        mal_id = (entry.get("media") or {}).get("idMal")
        if not mal_id:
            report.count("skipped")
            log_item(source="poll", entry=entry, mal_id=None, outcome="skipped",
                     detail="AniList has no MyAnimeList id for this title")
            advance(updated)
            continue
        try:
            current = await mal.list_status(int(mal_id))
        except MALUnavailable as exc:
            report.count("deferred")
            report.detail = f"stopped early: {exc}; the rest are picked up next run"
            return
        except MALError as exc:
            report.count("error")
            log_item(source="poll", entry=entry, mal_id=int(mal_id), outcome="error", detail=str(exc))
            advance(updated)
            continue

        plan = plan_mal_write(entry, current, settings)
        if plan.action != "write":
            report.count(plan.action)
            log_item(source="poll", entry=entry, mal_id=plan.mal_id, outcome=plan.action, detail=plan.reason)
            advance(updated)
            continue

        outcome = await gate.mal_save(mal, plan.mal_id, plan.fields, before=plan.before, after=plan.after)
        if outcome.result == RESULT_DEFERRED:
            report.count("deferred")
            report.detail = f"stopped early: {outcome.detail}; the rest are picked up next run"
            return
        status = "error" if not outcome.ok else ("would_write" if outcome.dry_run else "written")
        report.count(status)
        log_item(source="poll", entry=entry, mal_id=plan.mal_id, outcome=status,
                 detail=f"{outcome.detail} ({plan.reason})")
        advance(updated)


def _record(report: PollReport) -> None:
    _set("mal_last_poll", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    _set("mal_last_poll_summary", report.summary())
