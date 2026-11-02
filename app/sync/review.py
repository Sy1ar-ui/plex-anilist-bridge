"""The review queue: held and unmatched events, and the corrections that fix them.

Events the engine could not match confidently are grouped by show and season
(ten unmatched episodes of one show are one problem, not ten). A group is fixed
by linking it to an AniList title over an episode range, which is stored as a
manual correction in ``mapping_override`` -- the layer that beats every
dataset. The group's events are then run through the normal pipeline again,
so dry run, never-decrease and the rating hold all still apply.

The range uses the engine's one formula, ``entry_episode = plex_episode -
ep_offset``. The editor asks for it the way people think about it -- "Plex
episodes 14 to 26 are AniList episodes starting at 1" -- and stores
``ep_offset = ep_from - start``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app import db, repo
from app.mapping import seasons as season_helper

log = logging.getLogger(__name__)

REVIEW_STATUSES = ("held", "unmatched")
SCOPE_SHOW = "plex_guid"
SCOPE_TVDB = "tvdb"

_ANILIST_URL = re.compile(r"anilist\.co/anime/(\d+)", re.IGNORECASE)


@dataclass
class Group:
    """Every open review event for one show and season (or one movie)."""

    key: str
    event_ids: list[int]
    title: str
    media_kind: str
    season: int | None
    episodes: list[int]
    plex_guid: str | None
    tvdb_id: int | None
    status: str                      # held | unmatched (of the newest event)
    summary: str
    latest_id: int
    latest_at: str | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # Set by the UI: the events a correction already covers.
    covered_ids: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.media_kind == "movie":
            return self.title
        return f"{self.title} season {self.season if self.season is not None else '?'}"

    @property
    def episode_text(self) -> str:
        return compact_ranges(self.episodes)


# --------------------------------------------------------------------------
# reading the queue
# --------------------------------------------------------------------------
def _group_key(row: dict[str, Any]) -> str:
    who = row.get("show_plex_guid") or f"title:{row.get('show_title') or '?'}"
    if row.get("media_kind") == "movie":
        return f"{who}|movie"
    return f"{who}|{row.get('season')}"


def open_groups() -> list[Group]:
    """Every open group, the most recently watched first."""
    rows = db.query(
        "SELECT * FROM event WHERE status IN (?, ?) ORDER BY id DESC", REVIEW_STATUSES
    )
    groups: dict[str, Group] = {}
    tvdb_cache: dict[str, int | None] = {}
    for raw in rows:
        row = dict(raw)
        key = _group_key(row)
        group = groups.get(key)
        if group is None:
            guid = row.get("show_plex_guid")
            if guid not in tvdb_cache:
                tvdb_cache[guid] = _tvdb_for(guid)
            trace = _trace(row)
            group = Group(
                key=key,
                event_ids=[],
                title=row.get("show_title") or "unknown",
                media_kind=row.get("media_kind") or "episode",
                season=row.get("season"),
                episodes=[],
                plex_guid=guid,
                tvdb_id=tvdb_cache[guid],
                status=row["status"],
                summary=(trace or {}).get("summary") or row.get("detail") or "",
                latest_id=int(row["id"]),
                latest_at=row.get("viewed_at") or row.get("received_at"),
                candidates=_candidates(trace),
            )
            groups[key] = group
        group.event_ids.append(int(row["id"]))
        if row.get("episode") is not None and row["episode"] not in group.episodes:
            group.episodes.append(int(row["episode"]))
    for group in groups.values():
        group.episodes.sort()
        group.event_ids.sort()
    return list(groups.values())


def group_for_event(event_id: int) -> Group | None:
    row = db.query_one("SELECT * FROM event WHERE id = ?", (event_id,))
    if row is None:
        return None
    key = _group_key(dict(row))
    for group in open_groups():
        if group.key == key:
            return group
    return None


def open_count() -> int:
    return len(open_groups())


def _trace(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("trace_json"):
        return None
    try:
        return json.loads(row["trace_json"])
    except ValueError:
        return None


def _candidates(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the engine already considered: the chosen one first, no repeats."""
    if not trace:
        return []
    found: list[dict[str, Any]] = []
    for cand in [trace.get("chosen"), *(trace.get("candidates") or [])]:
        if cand and not any(c["anilist_id"] == cand["anilist_id"] for c in found):
            found.append(cand)
    return found


def _tvdb_for(plex_guid: str | None) -> int | None:
    if not plex_guid:
        return None
    row = db.query_one("SELECT tvdb_id FROM show WHERE plex_guid = ?", (plex_guid,))
    return int(row["tvdb_id"]) if row and row["tvdb_id"] else None


def season_size(plex_guid: str | None, season: int | None) -> int | None:
    """The last episode number Plex has for this season, if known."""
    if not plex_guid or season is None:
        return None
    info = season_helper.get_season_counts(plex_guid).get(int(season))
    if info is None:
        return None
    return int(info.max_episode or info.episode_count or 0) or None


# --------------------------------------------------------------------------
# the range editor
# --------------------------------------------------------------------------
def parse_anilist_ref(text: str | None) -> int | None:
    """An AniList id typed bare or pasted as an anilist.co URL."""
    text = (text or "").strip()
    if not text:
        return None
    match = _ANILIST_URL.search(text)
    if match:
        return int(match.group(1))
    if text.isdigit() and int(text) > 0:
        return int(text)
    return None


@dataclass
class RangeSpec:
    ep_from: int
    ep_to: int | None
    start: int

    @property
    def offset(self) -> int:
        return self.ep_from - self.start

    def covers(self, episode: int) -> bool:
        return episode >= self.ep_from and (self.ep_to is None or episode <= self.ep_to)


def parse_range(ep_from: Any, ep_to: Any, start: Any) -> RangeSpec:
    def whole(value: Any, name: str, *, optional: bool = False) -> int | None:
        text = str(value if value is not None else "").strip()
        if not text:
            if optional:
                return None
            raise ValueError(f"{name} is required")
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"{name} must be a whole number from 1")
        return int(text)

    first = whole(ep_from, "the first Plex episode")
    last = whole(ep_to, "the last Plex episode", optional=True)
    begin = whole(start, "the AniList episode it starts at")
    if last is not None and last < first:
        raise ValueError("the last Plex episode is before the first")
    return RangeSpec(first, last, begin)


def preview(
    spec: RangeSpec,
    *,
    season_end: int | None,
    total: int | None,
    watched: list[int],
    limit: int = 60,
) -> list[dict[str, Any]]:
    """Plex episode -> AniList episode, for every episode the range covers.

    Runs to ``ep_to``, else the season's last known episode, else the last
    watched one. Episodes past the AniList title's count are flagged.
    """
    end = spec.ep_to or season_end or max([spec.ep_from, *watched])
    rows = []
    for plex_ep in range(spec.ep_from, min(end, spec.ep_from + limit - 1) + 1):
        entry = plex_ep - spec.offset
        rows.append(
            {
                "plex": plex_ep,
                "entry": entry,
                "watched": plex_ep in watched,
                "past_end": bool(total) and entry > int(total),
                "invalid": entry < 1,
            }
        )
    return rows


def suggest_range(group: Group, candidate: dict[str, Any] | None) -> RangeSpec:
    """A starting point for the editor. The preview shows whether it is right."""
    if group.media_kind == "movie":
        return RangeSpec(1, 1, 1)
    offset = int((candidate or {}).get("ep_offset") or 0)
    if offset > 0:
        return RangeSpec(offset + 1, None, 1)
    return RangeSpec(1, None, 1)


# --------------------------------------------------------------------------
# corrections
# --------------------------------------------------------------------------
def save_override(
    group: Group,
    *,
    anilist_id: int,
    spec: RangeSpec,
    scope: str = SCOPE_SHOW,
    note: str = "",
) -> int:
    """Store a correction for this group. Replaces one with the same start."""
    if scope == SCOPE_TVDB:
        if not group.tvdb_id:
            raise ValueError("Plex gave this show no TVDB id, so it can only apply to this show")
        scope_key = str(group.tvdb_id)
    else:
        if not group.plex_guid:
            raise ValueError("Plex gave this item no GUID, so a correction cannot be keyed to it")
        scope, scope_key = SCOPE_SHOW, group.plex_guid

    # Movies are stored as season 1, episode 1: that is what the engine looks up.
    season = 1 if group.media_kind == "movie" else group.season
    if season is None:
        raise ValueError("Plex sent no season number for this item")
    if group.media_kind == "movie":
        spec = RangeSpec(1, 1, 1)

    with db.session() as conn:
        conn.execute(
            "DELETE FROM mapping_override"
            " WHERE scope_type = ? AND scope_key = ? AND season = ? AND ep_from = ?",
            (scope, scope_key, season, spec.ep_from),
        )
        cursor = conn.execute(
            "INSERT INTO mapping_override (scope_type, scope_key, season, ep_from, ep_to,"
            " anilist_id, ep_offset, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (scope, scope_key, season, spec.ep_from, spec.ep_to, anilist_id,
             spec.offset, note or f"linked from the review queue ({group.label})"),
        )
        return int(cursor.lastrowid)


def list_overrides() -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT o.*, m.title_english, m.title_romaji, m.episodes,"
        "  CASE o.scope_type"
        "    WHEN 'tvdb' THEN (SELECT title FROM show"
        "                      WHERE CAST(tvdb_id AS TEXT) = o.scope_key LIMIT 1)"
        "    ELSE (SELECT title FROM show WHERE plex_guid = o.scope_key)"
        "  END AS show_title"
        " FROM mapping_override o"
        " LEFT JOIN anilist_media_cache m ON m.anilist_id = o.anilist_id"
        " ORDER BY show_title, o.season, o.ep_from"
    )
    items = []
    for row in rows:
        item = dict(row)
        item["anilist_title"] = (
            item.get("title_english") or item.get("title_romaji") or f"AniList {item['anilist_id']}"
        )
        item["start"] = int(item["ep_from"]) - int(item["ep_offset"])
        items.append(item)
    return items


def delete_override(override_id: int) -> bool:
    with db.session() as conn:
        return bool(
            conn.execute("DELETE FROM mapping_override WHERE id = ?", (override_id,)).rowcount
        )


def dismiss(group: Group) -> int:
    """Take a group out of the queue without writing anything."""
    for event_id in group.event_ids:
        repo.finalise_event(
            event_id, status="skipped", detail="dismissed from the review queue"
        )
    return len(group.event_ids)


# --------------------------------------------------------------------------
# re-running the fixed events
# --------------------------------------------------------------------------
@dataclass
class ReprocessReport:
    outcomes: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.outcomes.values())

    def summary(self) -> str:
        if not self.outcomes:
            return "no events to re-run"
        parts = [f"{n} {status.replace('_', ' ')}" for status, n in sorted(self.outcomes.items())]
        return f"re-ran {self.total} event(s): " + ", ".join(parts)


async def reprocess(event_ids: list[int], *, anilist, plex=None, gate=None) -> ReprocessReport:
    """Run events through the pipeline again, oldest first, with their original input."""
    from app.sync.pipeline import handle_watch_event
    from app.sync.writegate import WriteGate

    gate = gate or WriteGate()
    report = ReprocessReport()
    for event_id in sorted(event_ids):
        event = repo.event_input(event_id)
        if event is None:
            continue
        try:
            result = await handle_watch_event(
                event, plex=plex, anilist=anilist, gate=gate, event_id=event_id
            )
            status = result.status
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the rest
            log.exception("re-running a reviewed event failed", extra={"event_id": event_id})
            repo.finalise_event(event_id, status="error", detail=f"re-run failed: {exc}")
            status = "error"
        report.outcomes[status] = report.outcomes.get(status, 0) + 1
    return report


def covered_events(group: Group, spec: RangeSpec) -> list[int]:
    """The group's events the new correction applies to."""
    if group.media_kind == "movie":
        return list(group.event_ids)
    rows = db.query(
        f"SELECT id, episode FROM event WHERE id IN ({','.join('?' * len(group.event_ids))})",
        group.event_ids,
    )
    return [int(r["id"]) for r in rows if r["episode"] is not None and spec.covers(int(r["episode"]))]


def compact_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] -> "1-3, 5, 7-8"."""
    if not numbers:
        return ""
    numbers = sorted(set(numbers))
    parts = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(parts)
