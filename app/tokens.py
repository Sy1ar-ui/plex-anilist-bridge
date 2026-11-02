"""OAuth token storage and status, shared by the AniList and MAL clients.

The status is kept in the database rather than inferred on the fly so that the
dashboard can show a broken token prominently instead of burying the failure in
a log line -- a specific complaint about the tools this replaces.

On storage, plainly: these tokens live in the SQLite file in /config with
restrictive file permissions. Encrypting them would mean keeping the key beside
the database in the same directory, which protects against nothing, so this
does not pretend to. Treat /config as sensitive.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app import db

log = logging.getLogger(__name__)

ANILIST = "anilist"
MAL = "mal"

STATUS_ABSENT = "absent"
STATUS_OK = "ok"
STATUS_EXPIRING = "expiring"
STATUS_BROKEN = "broken"


@dataclass
class TokenRecord:
    provider: str
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None
    account_name: str | None = None
    account_id: int | None = None
    status: str = STATUS_ABSENT
    last_error: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.access_token)

    def expires_in(self) -> timedelta | None:
        if self.expires_at is None:
            return None
        return self.expires_at - datetime.now(UTC)

    def healthy(self) -> bool:
        return self.present and self.status in (STATUS_OK, STATUS_EXPIRING)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def get(provider: str) -> TokenRecord:
    row = db.query_one(
        "SELECT provider, access_token, refresh_token, expires_at, account_name,"
        " account_id, status, last_error FROM oauth_token WHERE provider = ?",
        (provider,),
    )
    if not row:
        return TokenRecord(provider=provider)
    return TokenRecord(
        provider=row["provider"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=_parse_dt(row["expires_at"]),
        account_name=row["account_name"],
        account_id=row["account_id"],
        status=row["status"] or STATUS_ABSENT,
        last_error=row["last_error"],
    )


def save(
    provider: str,
    *,
    access_token: str | None,
    refresh_token: str | None = None,
    expires_at: datetime | None = None,
    account_name: str | None = None,
    account_id: int | None = None,
    status: str = STATUS_OK,
    last_error: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO oauth_token (provider, access_token, refresh_token, expires_at,"
        " account_name, account_id, status, last_error, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))"
        " ON CONFLICT (provider) DO UPDATE SET"
        "   access_token = excluded.access_token,"
        "   refresh_token = COALESCE(excluded.refresh_token, oauth_token.refresh_token),"
        "   expires_at = excluded.expires_at,"
        "   account_name = COALESCE(excluded.account_name, oauth_token.account_name),"
        "   account_id = COALESCE(excluded.account_id, oauth_token.account_id),"
        "   status = excluded.status,"
        "   last_error = excluded.last_error,"
        "   updated_at = excluded.updated_at",
        (
            provider,
            access_token,
            refresh_token,
            expires_at.isoformat() if expires_at else None,
            account_name,
            account_id,
            status,
            last_error,
        ),
    )


def mark_broken(provider: str, error: str) -> None:
    """Record a token failure so the UI can shout about it."""
    db.execute(
        "INSERT INTO oauth_token (provider, status, last_error, updated_at)"
        " VALUES (?, ?, ?, datetime('now'))"
        " ON CONFLICT (provider) DO UPDATE SET"
        "   status = excluded.status, last_error = excluded.last_error,"
        "   updated_at = excluded.updated_at",
        (provider, STATUS_BROKEN, error),
    )
    log.error("token broken", extra={"provider": provider, "error": error})


def all_statuses() -> dict[str, TokenRecord]:
    return {provider: get(provider) for provider in (ANILIST, MAL)}
