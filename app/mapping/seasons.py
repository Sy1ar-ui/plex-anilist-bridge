"""Per-season episode counts, read from the user's own Plex library.

Absolute-numbered shows (One Piece and friends) and seasons that continue a
single AniList entry both need to know how many episodes came before the
current season. The only source that is guaranteed to agree with what the user
actually watched is their library, so that is what we use -- and we downgrade
confidence when the library has gaps, because then the arithmetic cannot be
trusted.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import db


@dataclass(frozen=True)
class SeasonInfo:
    season: int
    episode_count: int
    min_episode: int | None
    max_episode: int | None
    contiguous: bool


def get_season_counts(plex_guid: str) -> dict[int, SeasonInfo]:
    rows = db.query(
        "SELECT season, episode_count, min_episode, max_episode, contiguous "
        "FROM show_season WHERE plex_guid = ? ORDER BY season",
        (plex_guid,),
    )
    return {
        int(r["season"]): SeasonInfo(
            season=int(r["season"]),
            episode_count=int(r["episode_count"]),
            min_episode=r["min_episode"],
            max_episode=r["max_episode"],
            contiguous=bool(r["contiguous"]),
        )
        for r in rows
    }


def save_season_counts(plex_guid: str, infos: list[SeasonInfo]) -> None:
    with db.session() as conn:
        conn.execute("DELETE FROM show_season WHERE plex_guid = ?", (plex_guid,))
        for info in infos:
            conn.execute(
                "INSERT INTO show_season (plex_guid, season, episode_count,"
                " min_episode, max_episode, contiguous, refreshed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    plex_guid,
                    info.season,
                    info.episode_count,
                    info.min_episode,
                    info.max_episode,
                    1 if info.contiguous else 0,
                ),
            )


@dataclass(frozen=True)
class PriorEpisodes:
    """How many episodes precede a season, and whether that number is safe."""

    count: int
    trustworthy: bool
    reason: str


def episodes_before(
    counts: dict[int, SeasonInfo], season: int, *, only_seasons: set[int] | None = None
) -> PriorEpisodes:
    """Sum episodes in seasons before `season`, skipping specials (season 0).

    `only_seasons` restricts the sum to particular seasons, which is what the
    "several Plex seasons continue one AniList entry" case needs.
    """
    relevant = [s for s in counts if 0 < s < season]
    if only_seasons is not None:
        relevant = [s for s in relevant if s in only_seasons]

    missing = [s for s in range(1, season) if s not in counts and (only_seasons is None or s in only_seasons)]
    gappy = [s for s in relevant if not counts[s].contiguous]

    total = sum(counts[s].episode_count for s in relevant)

    if missing:
        listed = ", ".join(str(s) for s in missing)
        return PriorEpisodes(
            total,
            False,
            f"season{'s' if len(missing) > 1 else ''} {listed} "
            f"{'are' if len(missing) > 1 else 'is'} not in the Plex library, "
            "so the episode count before this season is incomplete",
        )
    if gappy:
        listed = ", ".join(str(s) for s in gappy)
        return PriorEpisodes(
            total,
            False,
            f"season{'s' if len(gappy) > 1 else ''} {listed} "
            f"{'have' if len(gappy) > 1 else 'has'} missing episodes, "
            "so the running episode total may be wrong",
        )
    return PriorEpisodes(total, True, f"{total} episodes in the seasons before this one")
