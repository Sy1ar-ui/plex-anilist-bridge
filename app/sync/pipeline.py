"""From a watch event to an AniList write (or a review-queue entry).

Order matters here and is load-bearing:

1. the account filter has already run, upstream, before this is called;
2. enrichment (the show's external ids, which the webhook does not carry);
3. resolution, offline and deterministic;
4. the title-search fallback, only if nothing matched, and only ever
   "uncertain";
5. planning, which is where never-decrease lives;
6. the write gate, which is where dry-run lives.

Nothing writes to Plex, ever.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app import repo, settings_store
from app.anilist.client import AniListAuthError, AniListClient, AniListError
from app.anilist.rules import RuleSettings, plan_anilist_write
from app.mapping import engine
from app.mapping.titles import clean_title, season_variants, title_candidates
from app.models import Candidate, MatchResult, TraceStep, WatchEvent
from app.plex.client import PlexClient, PlexUnavailable
from app.sync.writegate import WriteGate

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

_TERMINAL = {
    STATUS_WRITTEN,
    STATUS_WOULD_WRITE,
    STATUS_NO_CHANGE,
    STATUS_HELD,
    STATUS_UNMATCHED,
    STATUS_SKIPPED,
}


@dataclass
class EventResult:
    event_id: int
    status: str
    detail: str
    is_new: bool = True
    match: MatchResult | None = None


async def handle_watch_event(
    event: WatchEvent,
    *,
    plex: PlexClient | None = None,
    anilist: AniListClient | None = None,
    gate: WriteGate | None = None,
) -> EventResult:
    settings = settings_store.all_settings()
    gate = gate or WriteGate()

    stored = repo.record_event(
        event, status=STATUS_RECEIVED, match=None, detail=f"received via {event.source}"
    )
    if not stored.is_new:
        previous = repo.event_status(stored.id)
        if previous in _TERMINAL:
            detail = f"already handled ({previous}); nothing written again"
            log.info("duplicate event", extra={"event_id": stored.id, "previous": previous})
            return EventResult(stored.id, STATUS_DUPLICATE, detail, is_new=False)

    def finish(status: str, detail: str, match: MatchResult | None = None, **kw) -> EventResult:
        repo.finalise_event(stored.id, status=status, detail=detail, match=match, **kw)
        return EventResult(stored.id, status, detail, stored.is_new, match)

    if not settings.get("sync_enabled", True):
        return finish(STATUS_SKIPPED, "syncing is turned off in settings")

    if event.media_kind == "episode" and event.season == 0 and settings.get("skip_specials", True):
        return finish(
            STATUS_SKIPPED,
            "Plex season 0 (specials); skipping specials is on in settings",
        )

    repo.upsert_show(event.show, rating_key=event.show_rating_key, section_id=None)

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
        match = await _title_search_fallback(anilist, event, match)

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
    except AniListAuthError as exc:
        return finish(STATUS_ERROR, f"AniList authentication failed: {exc}", match)
    except AniListError as exc:
        return finish(STATUS_ERROR, f"could not read AniList: {exc}", match)

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
    if outcome.result == "error":
        return finish(
            STATUS_ERROR,
            outcome.detail,
            match,
            anilist_id=candidate.anilist_id,
            entry_episode=candidate.entry_episode,
        )

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
