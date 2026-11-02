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

    async def mal_save(
        self,
        client,
        mal_id: int,
        fields: dict[str, Any],
        *,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        event_id: int | None = None,
    ) -> WriteOutcome:
        """Mirror one entry to MyAnimeList. Dry run applies exactly as for AniList."""
        from app.mal.client import MALAuthError, MALError, MALUnavailable

        diff = WriteDiff(anilist_id=mal_id, variables=dict(fields), before=before or {}, after=after,
                         reason="")
        return await self._write(
            TARGET_MAL,
            "PATCH my_list_status",
            lambda variables: client.update(mal_id, variables),
            diff,
            event_id=event_id,
            label=f"MyAnimeList {mal_id}",
            deferrable=(MALUnavailable, MALAuthError),
            failure=MALError,
        )

    async def _anilist_write(
        self,
        operation: str,
        send: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        diff: WriteDiff,
        *,
        event_id: int | None,
    ) -> WriteOutcome:
        return await self._write(
            TARGET_ANILIST, operation, send, diff, event_id=event_id,
            label=f"AniList {diff.anilist_id}",
            deferrable=(AniListUnavailable, AniListAuthError),
            failure=AniListError,
        )

    async def _write(
        self,
        target: str,
        operation: str,
        send: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        diff: WriteDiff,
        *,
        event_id: int | None,
        label: str,
        deferrable: tuple[type[Exception], ...],
        failure: type[Exception],
    ) -> WriteOutcome:
        def record(result: str, *, dry_run: bool, error: str | None = None) -> None:
            repo.log_write(
                event_id=event_id,
                target=target,
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
            log.info("dry run write", extra={"target": target, "id": diff.anilist_id,
                                             "variables": diff.variables})
            return WriteOutcome(RESULT_DRY_RUN, f"dry run: would have written {_describe(diff, label)}", True)

        name = "AniList" if target == TARGET_ANILIST else "MyAnimeList"
        try:
            response = await send(diff.variables)
        except deferrable as exc:
            record(RESULT_DEFERRED, dry_run=False, error=str(exc))
            log.warning("write deferred", extra={"target": target, "id": diff.anilist_id, "error": str(exc)})
            return WriteOutcome(RESULT_DEFERRED, f"{name} write deferred: {exc}", False, error=exc)
        except failure as exc:
            record(RESULT_ERROR, dry_run=False, error=str(exc))
            log.error("write failed", extra={"target": target, "id": diff.anilist_id, "error": str(exc)})
            return WriteOutcome(RESULT_ERROR, f"{name} write failed: {exc}", False, error=exc)

        record(RESULT_OK, dry_run=False)
        log.info("write", extra={"target": target, "id": diff.anilist_id, "variables": diff.variables})
        return WriteOutcome(RESULT_OK, f"wrote {_describe(diff, label)}", False, response)


def _describe(diff: WriteDiff, label: str | None = None) -> str:
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
    if "num_watched_episodes" in diff.variables:
        bits.append(f"episodes {diff.variables['num_watched_episodes']}")
    if "score" in diff.variables:
        bits.append(f"score {diff.variables['score']}")
    label = label or f"AniList {diff.anilist_id}"
    return f"{label}: " + ", ".join(bits) if bits else label
