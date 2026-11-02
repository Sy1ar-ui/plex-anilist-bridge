"""Plex webhook payload parsing and the account filter.

The Plex server is shared, so the account filter is the first thing that runs
and it reads exactly one field: ``Account.id``. An event from anyone else is
dropped before enrichment, before resolution, and before anything about what
they watched reaches the log -- only a counter moves.

Note what the payload does *not* contain: the show's external ids. There is no
``grandparentGuid``, and the episode-level ``Guid`` array is not dependable, so
the TVDB/TMDB ids have to be fetched from the Plex server using
``grandparentRatingKey``. That is why a server URL and token are required
rather than optional.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCROBBLE = "media.scrobble"


@dataclass(frozen=True)
class ParsedWebhook:
    event: str
    account_id: int | None
    media_kind: str                 # episode | movie | other
    rating_key: str | None
    show_rating_key: str | None
    season: int | None
    episode: int | None
    title: str | None
    show_title: str | None
    section_id: int | None
    guid: str | None                # plex://episode/... or plex://movie/...


def account_id_of(payload: dict[str, Any]) -> int | None:
    """Read only the account id. Deliberately the narrowest possible read."""
    account = payload.get("Account")
    if not isinstance(account, dict):
        return None
    raw = account.get("id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def event_of(payload: dict[str, Any]) -> str:
    value = payload.get("event")
    return value if isinstance(value, str) else ""


def is_own_account(payload: dict[str, Any], configured_account_id: int | None) -> bool:
    """True only when this event belongs to the configured account.

    If no account has been configured yet nothing is accepted, because
    accepting everything on a shared server is the exact failure this guards
    against.
    """
    if configured_account_id is None:
        return False
    return account_id_of(payload) == configured_account_id


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def parse(payload: dict[str, Any]) -> ParsedWebhook:
    metadata = payload.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    raw_type = metadata.get("type")
    kind = raw_type if raw_type in ("episode", "movie") else "other"

    return ParsedWebhook(
        event=event_of(payload),
        account_id=account_id_of(payload),
        media_kind=kind,
        rating_key=_str_or_none(metadata.get("ratingKey")),
        show_rating_key=_str_or_none(metadata.get("grandparentRatingKey")),
        season=_as_int(metadata.get("parentIndex")),
        episode=_as_int(metadata.get("index")),
        title=_str_or_none(metadata.get("title")),
        show_title=_str_or_none(metadata.get("grandparentTitle")) or _str_or_none(metadata.get("title")),
        section_id=_as_int(metadata.get("librarySectionID")),
        guid=_str_or_none(metadata.get("guid")),
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
