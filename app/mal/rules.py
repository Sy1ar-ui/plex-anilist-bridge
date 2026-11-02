"""What MyAnimeList should say for one AniList entry, as one pure function.

AniList is the source of truth; MAL is a mirror. Even so, the mirror is
careful in the same ways the Plex leg is:

* **Progress is never lowered** by the automatic poller, and a COMPLETED MAL
  entry is never moved back to watching. Such changes are flagged
  ("would lower") and only made from the full-reconcile preview, where the
  user ticks them one by one.
* **Only fields that differ are sent**, with absolute values.
* **Nothing is deleted**, and comments and tags are never written.
* A zero AniList score means "not scored", so it never clears a MAL score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["write", "no_change", "skipped"]

STATUS_MAP = {
    "CURRENT": "watching",
    "PLANNING": "plan_to_watch",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
    "PAUSED": "on_hold",
    # MAL marks a rewatch as COMPLETED with is_rewatching set.
    "REPEATING": "completed",
}


@dataclass(frozen=True)
class MalSettings:
    sync_score: bool = True
    sync_dates: bool = False


@dataclass
class MalPlan:
    action: Action
    reason: str
    mal_id: int | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] = field(default_factory=dict)
    lowers: list[str] = field(default_factory=list)   # what was held back, or would be lowered


def _date(fuzzy: dict[str, Any] | None) -> str | None:
    """AniList fuzzy date -> MAL "YYYY-MM-DD"; only complete dates are sent."""
    if not fuzzy or not all(fuzzy.get(k) for k in ("year", "month", "day")):
        return None
    return f"{int(fuzzy['year']):04d}-{int(fuzzy['month']):02d}-{int(fuzzy['day']):02d}"


def mal_score(anilist_score_100: Any) -> int | None:
    """AniList's 0-100 as MAL's 1-10. None when unscored."""
    try:
        value = float(anilist_score_100 or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Half up (85 -> 9), not Python's round-half-to-even (85 -> 8).
    return max(1, min(10, math.floor(value / 10 + 0.5)))


def desired(entry: dict[str, Any], settings: MalSettings) -> dict[str, Any]:
    """The MAL list status this AniList entry calls for."""
    status = str(entry.get("status") or "")
    want: dict[str, Any] = {
        "status": STATUS_MAP.get(status, "watching"),
        "is_rewatching": status == "REPEATING",
        "num_watched_episodes": int(entry.get("progress") or 0),
    }
    repeat = int(entry.get("repeat") or 0)
    if repeat:
        want["num_times_rewatched"] = repeat
    if settings.sync_score:
        score = mal_score(entry.get("score"))
        if score is not None:
            want["score"] = score
    if settings.sync_dates:
        for name, key in (("start_date", "startedAt"), ("finish_date", "completedAt")):
            day = _date(entry.get(key))
            if day:
                want[name] = day
    return want


def plan_mal_write(
    entry: dict[str, Any],
    current: dict[str, Any] | None,
    settings: MalSettings | None = None,
    *,
    allow_lower: bool = False,
) -> MalPlan:
    """Decide what to PATCH on MAL for one AniList entry.

    ``current`` is the normalised MAL list status, or None if the title is not
    on the MAL list. ``allow_lower`` is only ever True for an item the user
    approved in the full-reconcile preview.
    """
    settings = settings or MalSettings()
    media = entry.get("media") or {}
    mal_id = media.get("idMal")
    if not mal_id:
        return MalPlan("skipped", "AniList has no MyAnimeList id for this title")
    mal_id = int(mal_id)
    want = desired(entry, settings)

    if current is None:
        return MalPlan("write", f"not on MyAnimeList yet; adding as {want['status']}",
                       mal_id, dict(want), None, dict(want))

    fields = {k: v for k, v in want.items() if current.get(k) != v}

    # A rewatch restarts MAL's episode count, so fewer episodes is expected there.
    fewer_episodes = (
        "num_watched_episodes" in fields
        and want["num_watched_episodes"] < current["num_watched_episodes"]
        and not want["is_rewatching"]
    )
    leaving_completed = (
        "status" in fields and current.get("status") == "completed" and want["status"] != "completed"
    )
    lowers: list[str] = []
    if fewer_episodes:
        lowers.append(f"episodes {current['num_watched_episodes']} -> {want['num_watched_episodes']}")
    if leaving_completed:
        lowers.append(f"status completed -> {want['status']}")

    if not allow_lower:
        if fewer_episodes:
            fields.pop("num_watched_episodes")
        if leaving_completed:
            fields.pop("status")
            fields.pop("is_rewatching", None)

    after = {**current, **fields}
    if not fields:
        if lowers:
            return MalPlan(
                "skipped",
                "MyAnimeList is ahead (" + "; ".join(lowers) + "); progress is never lowered "
                "automatically. Approve it in the full reconcile if AniList is right.",
                mal_id, {}, current, after, lowers,
            )
        return MalPlan("no_change", "MyAnimeList already matches", mal_id, {}, current, after)

    bits = [f"{k} {current.get(k)!s} -> {v!s}" for k, v in fields.items()]
    reason = ", ".join(bits)
    if lowers and not allow_lower:
        reason += "; held back: " + "; ".join(lowers)
    return MalPlan("write", reason, mal_id, fields, current, after, lowers)
