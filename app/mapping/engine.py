"""The mapping engine: (Plex ids, season, episode) -> (AniList id, entry episode).

Layers are tried in priority order and the first one to produce a candidate
wins. Every attempt is recorded, so a failure can always be explained:

  1. manual override   (UI corrections; always wins)
  2. community dataset (PlexAniSync-Custom-Mappings, keyed by Plex GUID)
  3. Fribb/anime-lists (keyed by TVDB/TMDB id + season + episode offset)
  4. Sonarr            (id recovery only; feeds layer 3)
  5. title search      (always "uncertain"; run by the pipeline, since it needs
                        the network -- everything in this module is offline)

One arithmetic is used everywhere: ``entry_episode = episode - ep_offset``,
where ``ep_offset`` is the number of episodes preceding the AniList entry
inside the Plex season. The community dataset's ``start`` field is normalised
to ``start - 1`` on load so that it means the same thing.
"""
from __future__ import annotations

import logging

from app import db
from app.mapping import seasons as season_helper
from app.models import (
    LAYER_COMMUNITY,
    LAYER_FRIBB,
    LAYER_OVERRIDE,
    LAYER_SONARR,
    Candidate,
    MatchResult,
    TraceStep,
    WatchEvent,
)

log = logging.getLogger(__name__)

# Dataset entry kinds that can carry a running TV episode count.
_SERIES_KINDS = ("TV", "ONA", "OVA", "UNKNOWN", "SPECIAL")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def media_info(anilist_id: int) -> dict | None:
    row = db.query_one(
        "SELECT anilist_id, title_romaji, title_english, episodes, format, id_mal"
        " FROM anilist_media_cache WHERE anilist_id = ?",
        (anilist_id,),
    )
    return dict(row) if row else None


def _title_of(anilist_id: int) -> str:
    info = media_info(anilist_id)
    if not info:
        return f"AniList {anilist_id}"
    name = info.get("title_english") or info.get("title_romaji")
    return f'AniList {anilist_id} "{name}"' if name else f"AniList {anilist_id}"


def _pick_by_offset(
    rows: list[tuple[int, int]], episode: int
) -> tuple[tuple[int, int] | None, list[tuple[int, int]], list[tuple[int, int]]]:
    """Choose the entry covering `episode`.

    Entries are (anilist_id, ep_offset). The winner is the one with the largest
    offset still below `episode`, which is what keeps a split cour correct:
    with offsets 0 and 13, episode 13 belongs to the first entry while episode
    14 is episode 1 of the second.

    Returns the winner plus *both* kinds of loser, because "why this entry?" is
    a question the UI has to be able to answer:

    * ``too_late`` -- entries that start after this episode;
    * ``outranked`` -- entries that do cover this episode but are superseded by
      one that starts later and still covers it.
    """
    ordered = sorted(rows, key=lambda r: r[1], reverse=True)
    for index, row in enumerate(ordered):
        if row[1] < episode:
            return row, ordered[:index], ordered[index + 1 :]
    return None, ordered, []


def _validate_total(
    anilist_id: int, entry_episode: int, steps: list[TraceStep], layer: str
) -> tuple[bool, str]:
    """Reject an entry episode that runs past the entry's episode count."""
    info = media_info(anilist_id)
    total = info.get("episodes") if info else None
    if not total:
        return True, ""
    if entry_episode > int(total):
        reason = (
            f"episode {entry_episode} is past the {total} episodes of "
            f"{_title_of(anilist_id)}, so the dataset is probably missing the "
            "next part of this show"
        )
        steps.append(TraceStep(layer, "rejected", reason))
        return False, reason
    return True, ""


def _explain_losers(
    steps: list[TraceStep],
    layer: str,
    episode: int,
    chosen_offset: int,
    *,
    too_late: list[tuple[int, int]],
    outranked: list[tuple[int, int]],
) -> None:
    """Record why each entry that was not chosen lost.

    Both kinds are written down. Leaving out the outranked ones is what makes a
    split-cour decision look arbitrary: for episode 14 of a season split at 13,
    the interesting question is why part 1 was *not* used.
    """
    for other_id, other_offset in too_late:
        steps.append(
            TraceStep(
                layer,
                "rejected",
                f"{_title_of(other_id)} starts at episode {other_offset + 1},"
                f" after episode {episode}",
            )
        )
    for other_id, other_offset in outranked:
        steps.append(
            TraceStep(
                layer,
                "skipped",
                f"{_title_of(other_id)} starts at episode {other_offset + 1} and"
                f" also covers episode {episode}, but the entry starting at"
                f" episode {chosen_offset + 1} starts later and still covers it",
            )
        )


def _candidate(
    anilist_id: int, entry_episode: int, layer: str, offset: int, note: str = ""
) -> Candidate:
    info = media_info(anilist_id) or {}
    return Candidate(
        anilist_id=anilist_id,
        entry_episode=entry_episode,
        layer=layer,
        ep_offset=offset,
        note=note,
        title=info.get("title_english") or info.get("title_romaji"),
        total_episodes=info.get("episodes"),
    )


# --------------------------------------------------------------------------
# layer 1: manual overrides
# --------------------------------------------------------------------------
def _layer_override(event: WatchEvent, steps: list[TraceStep]) -> MatchResult | None:
    guid = event.show.plex_guid or ""
    tvdb = str(event.show.tvdb_id) if event.show.tvdb_id else ""
    episode = event.episode or 1
    rows = db.query(
        "SELECT anilist_id, ep_offset, ep_from, ep_to, note FROM mapping_override"
        " WHERE season = ?"
        "   AND ((scope_type = 'plex_guid' AND scope_key = ?)"
        "     OR (scope_type = 'tvdb' AND scope_key = ?))"
        "   AND ep_from <= ?"
        "   AND (ep_to IS NULL OR ep_to >= ?)"
        " ORDER BY ep_from DESC",
        (event.season, guid, tvdb, episode, episode),
    )
    if not rows:
        steps.append(
            TraceStep(LAYER_OVERRIDE, "miss", "no manual correction covers this episode")
        )
        return None

    row = rows[0]
    anilist_id = int(row["anilist_id"])
    offset = int(row["ep_offset"])
    entry_episode = episode - offset
    upper = row["ep_to"] if row["ep_to"] is not None else "end"
    detail = (
        f"manual correction S{event.season:02d}E{row['ep_from']}-{upper}"
        f" -> {_title_of(anilist_id)}, offset {offset}"
        f" -> entry episode {entry_episode}"
    )
    steps.append(TraceStep(LAYER_OVERRIDE, "hit", detail))
    return MatchResult(
        confidence="override",
        summary=detail,
        candidate=_candidate(
            anilist_id, entry_episode, LAYER_OVERRIDE, offset, row["note"] or ""
        ),
        steps=steps,
    )


# --------------------------------------------------------------------------
# layer 2: community dataset
# --------------------------------------------------------------------------
def _layer_community(event: WatchEvent, steps: list[TraceStep]) -> MatchResult | None:
    guid = event.show.plex_guid
    if not guid:
        steps.append(
            TraceStep(
                LAYER_COMMUNITY,
                "skipped",
                "no Plex show GUID, which this dataset is keyed on",
            )
        )
        return None

    rows = db.query(
        "SELECT season, ep_offset, anilist_id FROM ds_community"
        " WHERE plex_guid = ? ORDER BY season, ep_offset",
        (guid,),
    )
    if not rows:
        steps.append(
            TraceStep(LAYER_COMMUNITY, "miss", f"{guid} is not in the community dataset")
        )
        return None

    episode = event.episode or 1
    season_rows = [r for r in rows if int(r["season"]) == event.season]
    if not season_rows:
        covered = sorted({int(r["season"]) for r in rows})
        listed = ", ".join(str(s) for s in covered)
        steps.append(
            TraceStep(
                LAYER_COMMUNITY,
                "miss",
                f"dataset covers season(s) {listed} for this show"
                f" but not season {event.season}",
            )
        )
        return None

    chosen, too_late, outranked = _pick_by_offset(
        [(int(r["anilist_id"]), int(r["ep_offset"])) for r in season_rows], episode
    )
    if chosen is None:
        steps.append(
            TraceStep(
                LAYER_COMMUNITY,
                "rejected",
                f"every mapped entry for season {event.season}"
                f" starts after episode {episode}",
            )
        )
        return None

    anilist_id, offset = chosen
    _explain_losers(
        steps, LAYER_COMMUNITY, episode, offset, too_late=too_late, outranked=outranked
    )

    confidence = "confident"
    bits = [f"community dataset: {guid} S{event.season:02d} -> {_title_of(anilist_id)}"]

    # Several Plex seasons may continue one AniList entry (Gintama S1-S4 all map
    # to the same id), in which case the entry episode carries a running total.
    prior_seasons = {
        int(r["season"])
        for r in rows
        if 0 < int(r["season"]) < event.season and int(r["anilist_id"]) == anilist_id
    }
    if prior_seasons:
        counts = season_helper.get_season_counts(guid)
        prior = season_helper.episodes_before(
            counts, event.season, only_seasons=prior_seasons
        )
        entry_episode = prior.count + (episode - offset)
        listed = ", ".join(str(s) for s in sorted(prior_seasons))
        bits.append(
            f"season(s) {listed} map to the same entry, so it continues from"
            f" episode {prior.count + 1} ({prior.reason})"
        )
        if not prior.trustworthy:
            confidence = "uncertain"
    else:
        entry_episode = episode - offset

    bits.append(f"offset {offset} -> entry episode {entry_episode}")
    ok, reason = _validate_total(anilist_id, entry_episode, steps, LAYER_COMMUNITY)
    if not ok:
        confidence = "uncertain"
        bits.append(reason)

    summary = " -> ".join(bits)
    steps.append(TraceStep(LAYER_COMMUNITY, "hit", summary))
    return MatchResult(
        confidence=confidence,
        summary=summary,
        candidate=_candidate(anilist_id, entry_episode, LAYER_COMMUNITY, offset),
        steps=steps,
    )


# --------------------------------------------------------------------------
# layer 3: Fribb/anime-lists
# --------------------------------------------------------------------------
def _fribb_rows_for_season(
    *, tvdb_id: int | None, tmdb_id: int | None, season: int
) -> tuple[list[tuple[int, int]], str]:
    if tvdb_id:
        rows = db.query(
            "SELECT DISTINCT anilist_id, tvdb_offset AS off FROM ds_fribb"
            " WHERE tvdb_id = ? AND tvdb_season = ? AND anilist_id IS NOT NULL",
            (tvdb_id, season),
        )
        return [(int(r["anilist_id"]), int(r["off"])) for r in rows], f"tvdb {tvdb_id}"
    if tmdb_id:
        rows = db.query(
            "SELECT DISTINCT anilist_id, tmdb_offset AS off FROM ds_fribb"
            " WHERE tmdb_tv_id = ? AND tmdb_season = ? AND anilist_id IS NOT NULL",
            (tmdb_id, season),
        )
        return [(int(r["anilist_id"]), int(r["off"])) for r in rows], f"tmdb {tmdb_id}"
    return [], "no usable id"


def _fribb_absolute_rows(tvdb_id: int) -> list[tuple[int, int]]:
    """Rows whose upstream season was defaulttvdbseason="a" (absolute numbering).

    Fribb drops the non-numeric season, so the signal is that `season.tvdb` is
    absent entirely while a tvdb id is present.
    """
    placeholders = ",".join("?" for _ in _SERIES_KINDS)
    rows = db.query(
        "SELECT DISTINCT anilist_id, tvdb_offset AS off FROM ds_fribb"
        " WHERE tvdb_id = ? AND tvdb_season IS NULL AND anilist_id IS NOT NULL"
        f" AND kind IN ({placeholders})",
        (tvdb_id, *_SERIES_KINDS),
    )
    return [(int(r["anilist_id"]), int(r["off"])) for r in rows]


def _layer_fribb(event: WatchEvent, steps: list[TraceStep]) -> MatchResult | None:
    tvdb_id = event.show.tvdb_id
    tmdb_id = event.show.tmdb_id
    if not tvdb_id and not tmdb_id:
        steps.append(
            TraceStep(LAYER_FRIBB, "skipped", "Plex gave this show no TVDB or TMDB id")
        )
        return None

    episode = event.episode or 1
    season_rows, id_label = _fribb_rows_for_season(
        tvdb_id=tvdb_id, tmdb_id=tmdb_id, season=event.season
    )

    prior_note = ""
    confidence = "confident"
    lookup_episode = episode

    if not season_rows and tvdb_id:
        abs_rows = _fribb_absolute_rows(tvdb_id)
        if abs_rows:
            season_rows = abs_rows
            counts = season_helper.get_season_counts(event.show.plex_guid or "")
            prior = season_helper.episodes_before(counts, event.season)
            lookup_episode = prior.count + episode
            prior_note = (
                f"no season-{event.season} row but an absolute-numbering row"
                f" exists, so S{event.season:02d}E{episode:02d} is absolute"
                f" episode {lookup_episode} ({prior.reason})"
            )
            if not prior.trustworthy:
                confidence = "uncertain"

    if not season_rows:
        steps.append(
            TraceStep(
                LAYER_FRIBB,
                "miss",
                f"{id_label} has no entry for season {event.season} in the dataset",
            )
        )
        return None

    chosen, too_late, outranked = _pick_by_offset(season_rows, lookup_episode)
    if chosen is None:
        steps.append(
            TraceStep(
                LAYER_FRIBB,
                "rejected",
                f"every dataset entry for {id_label}"
                f" starts after episode {lookup_episode}",
            )
        )
        return None

    anilist_id, offset = chosen
    _explain_losers(
        steps,
        LAYER_FRIBB,
        lookup_episode,
        offset,
        too_late=too_late,
        outranked=outranked,
    )

    entry_episode = lookup_episode - offset
    bits = [f"{id_label} S{event.season:02d}E{episode:02d}"]
    if prior_note:
        bits.append(prior_note)
    bits.append(
        f"{_title_of(anilist_id)}, offset {offset} -> entry episode {entry_episode}"
    )

    ok, reason = _validate_total(anilist_id, entry_episode, steps, LAYER_FRIBB)
    if not ok:
        confidence = "uncertain"
        bits.append(reason)

    # More than one entry at the same offset means the dataset cannot tell them
    # apart, so a human should.
    same_offset = [r for r in season_rows if r[1] == offset]
    tied = [_candidate(a, lookup_episode - o, LAYER_FRIBB, o) for a, o in same_offset]
    if len(same_offset) > 1:
        confidence = "uncertain"
        names = ", ".join(_title_of(a) for a, _ in same_offset)
        bits.append(f"{len(same_offset)} dataset entries share offset {offset} ({names})")

    summary = " -> ".join(bits)
    steps.append(TraceStep(LAYER_FRIBB, "hit", summary))
    return MatchResult(
        confidence=confidence,
        summary=summary,
        candidate=_candidate(anilist_id, entry_episode, LAYER_FRIBB, offset),
        candidates=tied if len(tied) > 1 else [],
        steps=steps,
    )


# --------------------------------------------------------------------------
# movies
# --------------------------------------------------------------------------
def _resolve_movie(event: WatchEvent, steps: list[TraceStep]) -> MatchResult:
    # A manual correction wins for movies too. Movies are stored as season 1,
    # episode 1, which is how the range editor writes them.
    override = _layer_override(
        WatchEvent(
            source=event.source,
            received_at=event.received_at,
            show=event.show,
            media_kind="episode",
            season=1,
            episode=1,
            plex_account_id=event.plex_account_id,
            rating_key=event.rating_key,
        ),
        steps,
    )
    if override is not None:
        return override

    # The community dataset covers movies as well, keyed on plex://movie/<hex>.
    if event.show.plex_guid:
        row = db.query_one(
            "SELECT anilist_id, ep_offset FROM ds_community"
            " WHERE plex_guid = ? ORDER BY ep_offset LIMIT 1",
            (event.show.plex_guid,),
        )
        if row:
            anilist_id = int(row["anilist_id"])
            detail = (
                f"community dataset: {event.show.plex_guid}"
                f" -> {_title_of(anilist_id)}, entry episode 1"
            )
            steps.append(TraceStep(LAYER_COMMUNITY, "hit", detail))
            return MatchResult(
                confidence="confident",
                summary=detail,
                candidate=_candidate(anilist_id, 1, LAYER_COMMUNITY, 0),
                steps=steps,
            )
        steps.append(
            TraceStep(
                LAYER_COMMUNITY,
                "miss",
                f"{event.show.plex_guid} is not in the community dataset",
            )
        )

    found: list[int] = []
    tried: list[str] = []

    if event.show.tmdb_id:
        tried.append(f"tmdb {event.show.tmdb_id}")
        rows = db.query(
            "SELECT f.anilist_id FROM ds_fribb_ext e JOIN ds_fribb f USING (anidb_id)"
            " WHERE e.kind = 'tmdb_movie' AND e.value = ? AND f.anilist_id IS NOT NULL",
            (str(event.show.tmdb_id),),
        )
        found.extend(int(r["anilist_id"]) for r in rows)

    if not found and event.show.imdb_id:
        tried.append(f"imdb {event.show.imdb_id}")
        rows = db.query(
            "SELECT f.anilist_id FROM ds_fribb_ext e JOIN ds_fribb f USING (anidb_id)"
            " WHERE e.kind = 'imdb' AND e.value = ? AND f.anilist_id IS NOT NULL",
            (event.show.imdb_id,),
        )
        found.extend(int(r["anilist_id"]) for r in rows)

    unique = sorted(set(found))
    label = " / ".join(tried) if tried else "no movie ids from Plex"

    if not unique:
        detail = f"no dataset entry for {label}"
        steps.append(TraceStep(LAYER_FRIBB, "miss", detail))
        return MatchResult(confidence="unmatched", summary=detail, steps=steps)

    if len(unique) > 1:
        names = ", ".join(_title_of(a) for a in unique)
        detail = (
            f"{label} matches {len(unique)} dataset entries ({names}),"
            " so this needs a human"
        )
        steps.append(TraceStep(LAYER_FRIBB, "rejected", detail))
        return MatchResult(
            confidence="uncertain",
            summary=detail,
            candidate=_candidate(unique[0], 1, LAYER_FRIBB, 0),
            candidates=[_candidate(a, 1, LAYER_FRIBB, 0) for a in unique],
            steps=steps,
        )

    anilist_id = unique[0]
    detail = f"movie {label} -> {_title_of(anilist_id)}, entry episode 1"
    steps.append(TraceStep(LAYER_FRIBB, "hit", detail))
    return MatchResult(
        confidence="confident",
        summary=detail,
        candidate=_candidate(anilist_id, 1, LAYER_FRIBB, 0),
        steps=steps,
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def resolve(event: WatchEvent) -> MatchResult:
    """Resolve one watch event. Offline and deterministic.

    The title-search fallback is not run here because it needs the network; the
    pipeline appends it when this returns `unmatched`.
    """
    steps: list[TraceStep] = []

    if event.media_kind == "movie":
        return _resolve_movie(event, steps)

    if event.season is None or event.episode is None:
        detail = "Plex sent no season/episode number for this item"
        steps.append(TraceStep(LAYER_FRIBB, "skipped", detail))
        return MatchResult(confidence="unmatched", summary=detail, steps=steps)

    for layer in (_layer_override, _layer_community, _layer_fribb):
        result = layer(event, steps)
        if result is not None:
            return result

    steps.append(TraceStep(LAYER_SONARR, "skipped", "Sonarr id recovery is not configured"))
    detail = (
        f"nothing matched {event.show.describe()}"
        f" S{event.season:02d}E{event.episode:02d}:"
        " no manual correction, not in either mapping dataset"
    )
    return MatchResult(confidence="unmatched", summary=detail, steps=steps)
