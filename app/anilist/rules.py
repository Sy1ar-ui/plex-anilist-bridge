"""The AniList update rules, as one pure function.

Derived in part from PlexAniSync <https://github.com/RickDB/PlexAniSync>,
specifically the status-transition and never-decrease behaviour of
``Anilist.__update_entry`` in ``plexanisync/anilist.py``.

    Copyright (C) 2019-2025 RickDB and PlexAniSync contributors
    Copyright (C) 2026 plex-anilist-bridge contributors

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version. See the LICENSE file at the root of this repository.

Two things make this safe and are deliberate:

* **Writes are absolute.** The plan always carries ``progress = N``, never an
  increment, so replaying the same event cannot double-count. Idempotency is a
  property of the write, not of bookkeeping.
* **Scores and notes are never in the payload.** Not "set to the same value" --
  absent. There is a test that asserts this on the serialised variables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.models import AniListEntryState, WriteDiff

Action = Literal["write", "no_change", "skipped"]

STATUS_CURRENT = "CURRENT"
STATUS_PLANNING = "PLANNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_DROPPED = "DROPPED"
STATUS_PAUSED = "PAUSED"
STATUS_REPEATING = "REPEATING"

# Statuses that a progress increase should move to CURRENT.
_RESUMABLE = (None, "", STATUS_PLANNING, STATUS_PAUSED, STATUS_DROPPED)


@dataclass(frozen=True)
class RuleSettings:
    add_untracked: bool = True
    rewatch_action: str = "ignore"       # ignore | repeating
    set_dates: bool = True
    today: date = field(default_factory=date.today)


@dataclass
class Plan:
    action: Action
    reason: str
    diff: WriteDiff | None = None

    @property
    def writes(self) -> bool:
        return self.action == "write" and self.diff is not None


def _fuzzy(day: date) -> dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def _is_finished_airing(state: AniListEntryState) -> bool:
    """Only a finished series may be auto-completed.

    While a show is still releasing, AniList's episode count is the announced
    total and reaching it does not mean the series is over -- and for a show
    with no announced total it means nothing at all.
    """
    return state.media_status == "FINISHED"


def _reaches_end(state: AniListEntryState, entry_episode: int) -> bool:
    total = state.total_episodes
    return bool(total) and entry_episode >= int(total) and _is_finished_airing(state)


def plan_anilist_write(
    state: AniListEntryState,
    entry_episode: int,
    settings: RuleSettings | None = None,
) -> Plan:
    """Decide what, if anything, to write for one watched episode."""
    settings = settings or RuleSettings()
    before = state.snapshot()

    if entry_episode < 1:
        return Plan("skipped", f"computed entry episode {entry_episode} is not a real episode")

    # ---- not on the list yet --------------------------------------------
    if not state.exists:
        if not settings.add_untracked:
            return Plan(
                "skipped",
                "this title is not on your AniList list and adding untracked "
                "titles is turned off",
            )
        completed = _reaches_end(state, entry_episode)
        status = STATUS_COMPLETED if completed else STATUS_CURRENT
        variables: dict[str, Any] = {
            "mediaId": state.anilist_id,
            "status": status,
            "progress": entry_episode,
        }
        if settings.set_dates:
            variables["startedAt"] = _fuzzy(settings.today)
            if completed:
                variables["completedAt"] = _fuzzy(settings.today)
        reason = (
            f"not on your list yet, so adding it as {status} at episode {entry_episode}"
        )
        return Plan("write", reason, _make_diff(state, variables, before, reason))

    # ---- already completed: this is a rewatch ---------------------------
    if state.status == STATUS_COMPLETED:
        if settings.rewatch_action != "repeating":
            return Plan(
                "no_change",
                "already COMPLETED on AniList and rewatch syncing is set to ignore",
            )
        variables = {
            "mediaId": state.anilist_id,
            "status": STATUS_REPEATING,
            "progress": entry_episode,
        }
        reason = (
            f"already COMPLETED, so starting a rewatch: REPEATING at episode "
            f"{entry_episode}"
        )
        return Plan("write", reason, _make_diff(state, variables, before, reason))

    # ---- mid-rewatch ----------------------------------------------------
    if state.status == STATUS_REPEATING:
        if entry_episode <= state.progress:
            return Plan(
                "no_change",
                f"rewatch progress on AniList is already {state.progress}, "
                f"which is not behind episode {entry_episode}",
            )
        if _reaches_end(state, entry_episode):
            variables = {
                "mediaId": state.anilist_id,
                "status": STATUS_COMPLETED,
                "progress": entry_episode,
                "repeat": state.repeat + 1,
            }
            reason = (
                f"rewatch finished at episode {entry_episode}, so back to "
                f"COMPLETED with rewatch count {state.repeat + 1}"
            )
            return Plan("write", reason, _make_diff(state, variables, before, reason))
        variables = {
            "mediaId": state.anilist_id,
            "status": STATUS_REPEATING,
            "progress": entry_episode,
        }
        reason = f"rewatch progress {state.progress} -> {entry_episode}"
        return Plan("write", reason, _make_diff(state, variables, before, reason))

    # ---- the never-decrease rule ----------------------------------------
    if entry_episode <= state.progress:
        return Plan(
            "no_change",
            f"AniList already has progress {state.progress}, which is not behind "
            f"episode {entry_episode}; progress is never lowered",
        )

    # ---- a normal progress increase -------------------------------------
    completed = _reaches_end(state, entry_episode)
    if completed:
        status = STATUS_COMPLETED
    elif state.status in _RESUMABLE:
        status = STATUS_CURRENT
    else:
        status = state.status or STATUS_CURRENT

    variables = {
        "mediaId": state.anilist_id,
        "status": status,
        "progress": entry_episode,
    }
    if settings.set_dates:
        if status == STATUS_CURRENT and not state.started_at and not state.progress:
            variables["startedAt"] = _fuzzy(settings.today)
        if completed and not state.completed_at:
            variables["completedAt"] = _fuzzy(settings.today)

    bits = [f"progress {state.progress} -> {entry_episode}"]
    if status != state.status:
        bits.append(f"status {state.status or 'none'} -> {status}")
    if state.total_episodes and not completed and entry_episode >= int(state.total_episodes):
        bits.append(
            "left as CURRENT because the series is not marked finished on AniList yet"
        )
    reason = ", ".join(bits)
    return Plan("write", reason, _make_diff(state, variables, before, reason))


def _make_diff(
    state: AniListEntryState,
    variables: dict[str, Any],
    before: dict[str, Any],
    reason: str,
) -> WriteDiff:
    after = dict(before)
    after["exists"] = True
    after["status"] = variables.get("status", before.get("status"))
    after["progress"] = variables.get("progress", before.get("progress"))
    if "repeat" in variables:
        after["repeat"] = variables["repeat"]
    return WriteDiff(
        anilist_id=state.anilist_id,
        variables=variables,
        before=before,
        after=after,
        reason=reason,
    )
