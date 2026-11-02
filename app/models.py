"""Core value types shared across the pipeline."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

Confidence = Literal["override", "confident", "uncertain", "unmatched"]

# Layer names, in priority order. Used in traces and in the UI.
LAYER_OVERRIDE = "override"
LAYER_COMMUNITY = "community-dataset"
LAYER_FRIBB = "fribb-dataset"
LAYER_SONARR = "sonarr"
LAYER_TITLE = "title-search"


@dataclass(frozen=True)
class ShowIds:
    """External identity of a Plex show, as resolved from the Plex server."""

    plex_guid: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    title: str | None = None
    year: int | None = None
    # The show's folder name on disk, as Plex sees it. Used only to look the
    # show up in Sonarr when Plex gives no TVDB id.
    folder: str | None = None

    def describe(self) -> str:
        parts = []
        if self.tvdb_id:
            parts.append(f"tvdb {self.tvdb_id}")
        if self.tmdb_id:
            parts.append(f"tmdb {self.tmdb_id}")
        if self.imdb_id:
            parts.append(f"imdb {self.imdb_id}")
        if self.plex_guid:
            parts.append(self.plex_guid)
        return ", ".join(parts) if parts else "no external ids"


@dataclass(frozen=True)
class WatchEvent:
    """One thing the configured Plex account finished watching."""

    source: str                      # webhook | reconcile | replay
    received_at: datetime
    show: ShowIds
    media_kind: str = "episode"      # episode | movie
    season: int | None = None
    episode: int | None = None
    plex_account_id: int | None = None
    rating_key: str | None = None
    show_rating_key: str | None = None
    viewed_at: datetime | None = None

    def label(self) -> str:
        title = self.show.title or "unknown"
        if self.media_kind == "movie":
            return title
        return f"{title} S{self.season or 0:02d}E{self.episode or 0:02d}"

    def to_json(self) -> str:
        """Everything needed to run this event through the pipeline again later."""
        data = asdict(self)
        data["received_at"] = self.received_at.isoformat()
        data["viewed_at"] = self.viewed_at.isoformat() if self.viewed_at else None
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str) -> "WatchEvent":
        data = json.loads(raw)
        show = ShowIds(**(data.pop("show", None) or {}))
        received = datetime.fromisoformat(data.pop("received_at"))
        viewed_raw = data.pop("viewed_at", None)
        viewed = datetime.fromisoformat(viewed_raw) if viewed_raw else None
        return cls(show=show, received_at=received, viewed_at=viewed, **data)


@dataclass
class TraceStep:
    layer: str
    outcome: str                     # hit | miss | rejected | skipped
    detail: str


@dataclass
class Candidate:
    """A possible AniList target for an event."""

    anilist_id: int
    entry_episode: int
    layer: str
    ep_offset: int = 0
    note: str = ""
    title: str | None = None
    total_episodes: int | None = None


@dataclass
class MatchResult:
    confidence: Confidence
    summary: str
    candidate: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)
    steps: list[TraceStep] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.candidate is not None

    def trace_json(self) -> str:
        return json.dumps(
            {
                "confidence": self.confidence,
                "summary": self.summary,
                "chosen": asdict(self.candidate) if self.candidate else None,
                "candidates": [asdict(c) for c in self.candidates],
                "steps": [asdict(s) for s in self.steps],
            },
            default=str,
        )


@dataclass
class AniListEntryState:
    """The remote state of one AniList list entry, before we touch it."""

    anilist_id: int
    exists: bool = False
    list_entry_id: int | None = None
    status: str | None = None
    progress: int = 0
    repeat: int = 0
    total_episodes: int | None = None
    media_status: str | None = None     # FINISHED | RELEASING | ...
    title: str | None = None
    started_at: dict[str, Any] | None = None
    completed_at: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "status": self.status,
            "progress": self.progress,
            "repeat": self.repeat,
            "total_episodes": self.total_episodes,
        }


@dataclass
class WriteDiff:
    """A planned, absolute write. Never an increment."""

    anilist_id: int
    variables: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str
