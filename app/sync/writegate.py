"""The only place in the application that writes to AniList or MAL.

Every mutation goes through here, which gives dry-run one implementation
instead of a flag checked in a dozen places. In dry-run the exact request that
*would* have been sent is recorded, together with the remote state before it,
so the UI can show the real payload rather than a paraphrase.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
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
        """An automatic write from the pipeline. Never carries a score."""
        return await self._anilist_write(
            "SaveMediaListEntry", client.save_entry, diff, event_id=event_id
        )

    async def anilist_rated_completion(
        self,
        client: AniListClient,
        diff: WriteDiff,
        *,
        event_id: int | None,
    ) -> WriteOutcome:
        """Complete a title the user rated in the UI. Dry run applies here too."""
        return await self._anilist_write(
            "SaveMediaListEntry (rated completion)", client.save_rated_completion, diff, event_id=event_id
        )

    async def _anilist_write(
        self,
        operation: str,
        send: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        diff: WriteDiff,
        *,
        event_id: int | None,
    ) -> WriteOutcome:
        def record(result: str, *, dry_run: bool, error: str | None = None) -> None:
            repo.log_write(
                event_id=event_id,
                target=TARGET_ANILIST,
                dry_run=dry_run,
                operation=operation,
                request=diff.variables,
                before=diff.before,
                after=diff.after,
                result=result,
                error=error,
            )

        if self.dry_run:
            record(RESULT_DRY_RUN, dry_run=True)
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
            response = await send(diff.variables)
        except (AniListUnavailable, AniListAuthError) as exc:
            record(RESULT_DEFERRED, dry_run=False, error=str(exc))
            log.warning(
                "anilist write deferred",
                extra={"anilist_id": diff.anilist_id, "error": str(exc)},
            )
            return WriteOutcome(
                RESULT_DEFERRED, f"AniList write deferred: {exc}", False, error=exc
            )
        except AniListError as exc:
            record(RESULT_ERROR, dry_run=False, error=str(exc))
            log.error(
                "anilist write failed",
                extra={"anilist_id": diff.anilist_id, "error": str(exc)},
            )
            return WriteOutcome(RESULT_ERROR, f"AniList write failed: {exc}", False, error=exc)

        record(RESULT_OK, dry_run=False)
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
    if "scoreRaw" in diff.variables:
        bits.append(f"rating {diff.variables['scoreRaw'] / 10:g}/10")
    if "completedAt" in diff.variables:
        day = diff.variables["completedAt"]
        bits.append(f"completed {day['year']:04d}-{day['month']:02d}-{day['day']:02d}")
    if "repeat" in diff.variables:
        bits.append(f"rewatch count {diff.variables['repeat']}")
    return f"AniList {diff.anilist_id}: " + ", ".join(bits) if bits else f"AniList {diff.anilist_id}"
