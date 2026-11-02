"""The only place in the application that writes to AniList or MAL.

Every mutation goes through here, which gives dry-run one implementation
instead of a flag checked in a dozen places. In dry-run the exact request that
*would* have been sent is recorded, together with the remote state before it,
so the UI can show the real payload rather than a paraphrase.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app import repo, settings_store
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListError,
    AniListUnavailable,
)
from app.models import WriteDiff

log = logging.getLogger(__name__)

TARGET_ANILIST = "anilist"
TARGET_MAL = "mal"

RESULT_OK = "ok"
RESULT_DRY_RUN = "dry_run"
RESULT_ERROR = "error"
# AniList was unreachable or the token needs re-authorising. Nothing was
# written; the event is retried later. Safe because every write is absolute.
RESULT_DEFERRED = "deferred"


@dataclass
class WriteOutcome:
    result: str
    detail: str
    dry_run: bool
    response: dict[str, Any] | None = None
    error: AniListError | None = None

    @property
    def ok(self) -> bool:
        return self.result in (RESULT_OK, RESULT_DRY_RUN)

    @property
    def performed(self) -> bool:
        return self.result == RESULT_OK


class WriteGate:
    def __init__(self, *, dry_run: bool | None = None) -> None:
        # Read the setting now, so one pipeline run is internally consistent,
        # but never cache it across runs.
        self.dry_run = settings_store.is_dry_run() if dry_run is None else dry_run

    async def anilist_save(
        self,
        client: AniListClient,
        diff: WriteDiff,
        *,
        event_id: int | None,
    ) -> WriteOutcome:
        if self.dry_run:
            repo.log_write(
                event_id=event_id,
                target=TARGET_ANILIST,
                dry_run=True,
                operation="SaveMediaListEntry",
                request=diff.variables,
                before=diff.before,
                after=diff.after,
                result=RESULT_DRY_RUN,
            )
            detail = f"dry run: would have written {_describe(diff)}"
            log.info(
                "dry run write",
                extra={
                    "target": TARGET_ANILIST,
                    "anilist_id": diff.anilist_id,
                    "variables": diff.variables,
                },
            )
            return WriteOutcome(RESULT_DRY_RUN, detail, True)

        try:
            response = await client.save_entry(diff.variables)
        except (AniListUnavailable, AniListAuthError) as exc:
            repo.log_write(
                event_id=event_id,
                target=TARGET_ANILIST,
                dry_run=False,
                operation="SaveMediaListEntry",
                request=diff.variables,
                before=diff.before,
                after=diff.after,
                result=RESULT_DEFERRED,
                error=str(exc),
            )
            log.warning(
                "anilist write deferred",
                extra={"anilist_id": diff.anilist_id, "error": str(exc)},
            )
            return WriteOutcome(
                RESULT_DEFERRED, f"AniList write deferred: {exc}", False, error=exc
            )
        except AniListError as exc:
            repo.log_write(
                event_id=event_id,
                target=TARGET_ANILIST,
                dry_run=False,
                operation="SaveMediaListEntry",
                request=diff.variables,
                before=diff.before,
                after=diff.after,
                result=RESULT_ERROR,
                error=str(exc),
            )
            log.error(
                "anilist write failed",
                extra={"anilist_id": diff.anilist_id, "error": str(exc)},
            )
            return WriteOutcome(RESULT_ERROR, f"AniList write failed: {exc}", False, error=exc)

        repo.log_write(
            event_id=event_id,
            target=TARGET_ANILIST,
            dry_run=False,
            operation="SaveMediaListEntry",
            request=diff.variables,
            before=diff.before,
            after=diff.after,
            result=RESULT_OK,
        )
        detail = f"wrote {_describe(diff)}"
        log.info(
            "anilist write",
            extra={"anilist_id": diff.anilist_id, "variables": diff.variables},
        )
        return WriteOutcome(RESULT_OK, detail, False, response)


def _describe(diff: WriteDiff) -> str:
    status = diff.variables.get("status")
    progress = diff.variables.get("progress")
    bits = []
    if progress is not None:
        bits.append(f"progress {progress}")
    if status:
        bits.append(f"status {status}")
    if "repeat" in diff.variables:
        bits.append(f"rewatch count {diff.variables['repeat']}")
    return f"AniList {diff.anilist_id}: " + ", ".join(bits) if bits else f"AniList {diff.anilist_id}"
