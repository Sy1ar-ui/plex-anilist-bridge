"""From a watch event to an AniList write (or a review-queue entry).

Order matters here and is load-bearing:

1. the account filter has already run, upstream, before this is called;
2. enrichment (the show's external ids, which the webhook does not carry);
3. resolution, offline and deterministic;
4. the title-search fallback, only if nothing matched, and only ever
   "uncertain";
5. planning, which is where never-decrease lives;
6. the write gate, which is where dry-run lives.

Finishing a series can be held for a rating (``rate_before_complete``): the
final episode is written as one short of the total, the event ends as
``awaiting_rating``, and :mod:`app.sync.completion` writes the completion,
dated the day the final episode was watched, once the user rates it.

If AniList is down, throttling hard, or the token needs re-authorising, the
event is **deferred** rather than failed: it keeps its place, and the retry
job (:mod:`app.sync.retry`) runs it through this same function again once
AniList is back. That is safe because every write is absolute.

Nothing writes to Plex, ever.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app import notify, repo, settings_store
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListError,
    AniListUnavailable,
)
from app.anilist.rules import RuleSettings, plan_anilist_write
from app.mapping import engine
from app.mapping.titles import clean_title, season_variants, title_candidates
from app.models import Candidate, MatchResult, TraceStep, WatchEvent
from app.plex.client import PlexClient, PlexUnavailable
from app.sync import completion, ignored
from app.sync.writegate import RESULT_DEFERRED, WriteGate

log = logging.getLogger(__name__)

STATUS_RECEIVED = "received"
STATUS_WRITTEN = "written"
STATUS_WOULD_WRITE = "would_write"
STATUS_NO_CHANGE = "no_change"
STATUS_HELD = "held"
STATUS_UNMATCHED = "unmatched"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"
STATUS_DUPLICATE = "duplicate"
STATUS_AWAITING_RATING = "awaiting_rating"
STATUS_DEFERRED = repo.STATUS_DEFERRED

# Deferred events back off per event: 5 min, 10, 20, 40, then hourly.
RETRY_BASE = timedelta(minutes=5)
RETRY_CAP = timedelta(hours=1)

_TERMINAL = {
    STATUS_WRITTEN,
    STATUS_WOULD_WRITE,
    STATUS_NO_CHANGE,
    STATUS_HELD,
    STATUS_UNMATCHED,
    STATUS_SKIPPED,
    STATUS_AWAITING_RATING,
}


@dataclass
class EventResult:
    event_id: int
    status: str
    detail: str
    is_new: bool = True
    match: MatchResult | None = None


def watched_on(event: WatchEvent) -> date:
    """The local calendar day the item was watched.

    Used for AniList's start and completion dates, so an event processed late
    (deferred through an outage, found by the reconcile sweep, or held for a
    rating) is still dated the day it was watched. Local time follows ``TZ``.
    """
    moment = event.viewed_at or event.received_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().date()


def retry_delay(attempts: int) -> timedelta:
    return min(RETRY_BASE * (2 ** max(0, attempts - 1)), RETRY_CAP)


async def handle_watch_event(
    event: WatchEvent,
    *,
    plex: PlexClient | None = None,
    anilist: AniListClient | None = None,
    gate: WriteGate | None = None,
    event_id: int | None = None,
) -> EventResult:
    """Run one event through the pipeline.

    ``event_id`` is given when retrying an event that already has a row; the
    row is updated in place rather than looked up by its dedupe key.
    """
    settings = settings_store.all_settings()
    gate = gate or WriteGate()

    if event_id is not None:
        stored = repo.StoredEvent(event_id, False)
    else:
        stored = repo.record_event(
            event, status=STATUS_RECEIVED, match=None, detail=f"received via {event.source}"
        )
    if not stored.is_new and event_id is None:
        previous = repo.event_status(stored.id)
        if previous in _TERMINAL:
            detail = f"already handled ({previous}); nothing written again"
            log.info("duplicate event", extra={"event_id": stored.id, "previous": previous})
            return EventResult(stored.id, STATUS_DUPLICATE, detail, is_new=False)

    def finish(status: str, detail: str, match: MatchResult | None = None, **kw) -> EventResult:
        repo.finalise_event(stored.id, status=status, detail=detail, match=match, **kw)
        if status in (STATUS_HELD, STATUS_UNMATCHED):
            notify.review_needed(
                event_id=stored.id, show_title=event.show.title, season=event.season,
                media_kind=event.media_kind, plex_guid=event.show.plex_guid,
                reason=(match.summary if match else detail),
            )
        return EventResult(stored.id, status, detail, stored.is_new, match)

    def defer(exc: AniListError, match: MatchResult | None = None, **kw) -> EventResult:
        """Park the event until AniList is usable again, or give up if it is old."""
        attempts = repo.event_attempts(stored.id) + 1
        now = datetime.now(UTC)
        give_up_days = _int_setting(settings, "anilist_retry_give_up_days", 7)
        received = event.received_at
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        waiting_for = (
            "AniList re-authorisation" if isinstance(exc, AniListAuthError) else "AniList"
        )

        if now - received > timedelta(days=give_up_days):
            result = finish(
                STATUS_ERROR,
                f"gave up after {attempts} attempt(s) over {give_up_days} day(s) "
                f"waiting for {waiting_for}: {exc}. Use Retry on this event to try again.",
                match,
                **kw,
            )
            repo.record_attempts(stored.id, attempts)
            return result

        when = now + retry_delay(attempts)
        retry_at = getattr(exc, "retry_at", None)
        if retry_at:
            when = max(when, datetime.fromtimestamp(retry_at, UTC))
        detail = f"deferred (attempt {attempts}), waiting for {waiting_for}: {exc}"
        result = finish(STATUS_DEFERRED, detail, match, **kw)
        repo.schedule_retry(stored.id, attempts=attempts, next_attempt_at=when)
        log.warning(
            "event deferred",
            extra={"event_id": stored.id, "attempts": attempts, "error": str(exc)},
        )
        return result

    if not settings.get("sync_enabled", True):
        return finish(STATUS_SKIPPED, "syncing is turned off in settings")

    if event.media_kind == "episode" and event.season == 0 and settings.get("skip_specials", True):
        return finish(
            STATUS_SKIPPED,
            "Plex season 0 (specials); skipping specials is on in settings",
        )

    repo.upsert_show(event.show, rating_key=event.show_rating_key, section_id=None)

    # A show the user dropped never reaches the matcher again. Checked here
    # rather than in the review queue so a dropped show stops costing a lookup
    # and stops reappearing every time another episode is watched.
    dropped = ignored.match(event.show)
    if dropped is not None:
        return finish(
            STATUS_SKIPPED,
            f"dropped: you chose to ignore this show ({dropped.describe()})",
        )

    # ---- enrichment: per-season episode counts --------------------------
    if plex is not None and event.show.plex_guid and event.show_rating_key:
        if repo.season_counts_are_stale(event.show.plex_guid):
            try:
                infos = await _season_counts(plex, event.show_rating_key)
                repo.store_season_counts(event.show.plex_guid, infos)
            except PlexUnavailable as exc:
                log.warning("could not read season counts", extra={"error": str(exc)})

    # ---- resolution ------------------------------------------------------
    match = engine.resolve(event)

    if match.confidence == "unmatched" and anilist is not None:
        try:
            match = await _title_search_fallback(anilist, event, match)
        except (AniListUnavailable, AniListAuthError) as exc:
            # Without the search this would land as "unmatched", which is
            # wrong: it might well match once AniList answers.
            match.steps.append(
                TraceStep("title-search", "skipped", f"AniList unavailable: {exc}")
            )
            return defer(exc, match)

    if match.confidence == "unmatched" or match.candidate is None:
        return finish(STATUS_UNMATCHED, match.summary, match)

    if match.confidence == "uncertain" and settings.get("uncertain_action", "hold") == "hold":
        return finish(
            STATUS_HELD,
            f"held for review: {match.summary}",
            match,
            anilist_id=match.candidate.anilist_id,
            entry_episode=match.candidate.entry_episode,
        )

    if anilist is None:
        return finish(
            STATUS_ERROR,
            "matched, but AniList is not connected so nothing could be written",
            match,
            anilist_id=match.candidate.anilist_id,
            entry_episode=match.candidate.entry_episode,
        )

    candidate = match.candidate

    # ---- remote state ----------------------------------------------------
    try:
        state, media = await anilist.media_with_entry(candidate.anilist_id)
    except (AniListUnavailable, AniListAuthError) as exc:
        return defer(
            exc,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )
    except AniListError as exc:
        return finish(
            STATUS_ERROR,
            f"could not read AniList: {exc}",
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

    repo.cache_media(media)

    # Now that the real episode count is known, re-check the arithmetic. The
    # engine may not have been able to: its cache can be cold on a first sight.
    if state.total_episodes and candidate.entry_episode > int(state.total_episodes):
        detail = (
            f"episode {candidate.entry_episode} is past the "
            f"{state.total_episodes} episodes of {state.title or candidate.anilist_id}, "
            "so the mapping is wrong or the dataset is missing a later part"
        )
        match.steps.append(TraceStep(candidate.layer, "rejected", detail))
        match.confidence = "uncertain"
        return finish(
            STATUS_HELD,
            f"held for review: {detail}",
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

    # ---- planning --------------------------------------------------------
    plan = plan_anilist_write(
        state,
        candidate.entry_episode,
        RuleSettings(
            add_untracked=bool(settings.get("add_untracked", True)),
            rewatch_action=str(settings.get("rewatch_action", "ignore")),
            set_dates=bool(settings.get("set_dates", True)),
            today=watched_on(event),
            hold_completion_for_rating=bool(settings.get("rate_before_complete", True)),
        ),
    )

    if plan.action == "skipped":
        return finish(
            STATUS_SKIPPED,
            plan.reason,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )
    def await_rating(detail: str) -> EventResult:
        total = int(state.total_episodes or candidate.entry_episode)
        completion.record_pending(
            anilist_id=candidate.anilist_id,
            event_id=stored.id,
            title=state.title or candidate.title or event.show.title,
            total_episodes=total,
            completed_on=watched_on(event),
        )
        return finish(
            STATUS_AWAITING_RATING,
            detail,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

    if plan.awaiting_rating and plan.diff is None:
        return await_rating(plan.reason)

    if plan.action == "no_change" or plan.diff is None:
        return finish(
            STATUS_NO_CHANGE,
            plan.reason,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

    # ---- the write -------------------------------------------------------
    outcome = await gate.anilist_save(anilist, plan.diff, event_id=stored.id)
    if outcome.result == RESULT_DEFERRED and outcome.error is not None:
        return defer(
            outcome.error,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )
    if outcome.result == "error":
        return finish(
            STATUS_ERROR,
            outcome.detail,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

    if plan.awaiting_rating:
        detail = f"{outcome.detail} ({plan.reason})"
        if match.confidence == "uncertain":
            detail = f"flagged as uncertain but applied: {detail}"
        return await_rating(detail)

    status = STATUS_WOULD_WRITE if outcome.dry_run else STATUS_WRITTEN
    detail = f"{outcome.detail} ({plan.reason})"
    if match.confidence == "uncertain":
        detail = f"flagged as uncertain but applied: {detail}"
    return finish(
        status,
        detail,
        match,
        anilist_id=candidate.anilist_id,
        entry_episode=candidate.entry_episode,
    )


def _int_setting(settings: dict, key: str, default: int) -> int:
    try:
        return max(1, int(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


async def _season_counts(plex: PlexClient, show_rating_key: str):
    from app.plex import client as plex_client

    return await plex_client.run(plex.season_counts_sync, show_rating_key)


async def _title_search_fallback(
    anilist: AniListClient, event: WatchEvent, match: MatchResult
) -> MatchResult:
    """Last resort: search AniList by title.

    This is a heuristic, so it never produces a confident match. An exact
    cleaned-title hit becomes "uncertain" (held for review by default) and
    anything weaker is offered to the review queue as a suggestion.
    """
    title = event.show.title
    if not title:
        match.steps.append(
            TraceStep("title-search", "skipped", "Plex gave this item no show title")
        )
        return match

    season = event.season or 1
    searches = season_variants(title, season) if season > 1 else [title]
    searches = searches[:3]

    found: list[Candidate] = []
    wanted = {clean_title(s) for s in title_candidates(*searches)}

    for term in searches:
        try:
            results = await anilist.search(term, per_page=10)
        except (AniListUnavailable, AniListAuthError):
            raise  # the caller defers the whole event
        except AniListError as exc:
            match.steps.append(
                TraceStep("title-search", "skipped", f"AniList search failed: {exc}")
            )
            return match
        for media in results:
            names = media.get("title") or {}
            candidate = Candidate(
                anilist_id=int(media["id"]),
                entry_episode=event.episode or 1,
                layer="title-search",
                ep_offset=0,
                title=names.get("english") or names.get("romaji"),
                total_episodes=media.get("episodes"),
                note=f'matched by searching "{term}"',
            )
            if not any(c.anilist_id == candidate.anilist_id for c in found):
                found.append(candidate)
            repo.cache_media(media)

    if not found:
        match.steps.append(
            TraceStep("title-search", "miss", f'AniList search found nothing for "{title}"')
        )
        return match

    exact = [
        c
        for c in found
        if clean_title(c.title or "") in wanted
        or any(clean_title(c.title or "") == clean_title(s) for s in searches)
    ]

    match.candidates = found[:10]
    if exact:
        chosen = exact[0]
        summary = (
            f'title search matched "{chosen.title}" (AniList {chosen.anilist_id}) '
            f"for S{season:02d}E{event.episode or 1:02d}; title matching is a guess, "
            "so this is only ever uncertain"
        )
        match.steps.append(TraceStep("title-search", "hit", summary))
        match.confidence = "uncertain"
        match.candidate = chosen
        match.summary = summary
        return match

    names = ", ".join(f"{c.title} ({c.anilist_id})" for c in found[:5])
    summary = (
        f'nothing matched by id, and a title search for "{title}" returned only '
        f"near misses ({names}), so this needs a human"
    )
    match.steps.append(TraceStep("title-search", "rejected", summary))
    match.summary = summary
    return match
