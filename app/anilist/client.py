"""AniList GraphQL client: rate-limited, retrying, and honest about failures."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app import config, tokens
from app.anilist import queries
from app.models import AniListEntryState
from app.ratelimit import RateLimiter

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 4


class AniListError(RuntimeError):
    """A request to AniList failed in a way the caller should surface."""


class AniListAuthError(AniListError):
    """The token was rejected. Needs a re-auth, not a retry."""


# One limiter per process so every caller shares the same budget. AniList is
# currently capped at 30/min; the limiter adopts whatever the headers report.
_limiter = RateLimiter(30, name="anilist")


def limiter() -> RateLimiter:
    return _limiter


class AniListClient:
    def __init__(
        self,
        access_token: str,
        *,
        http: httpx.AsyncClient | None = None,
        pacer: RateLimiter | None = None,
    ) -> None:
        self._token = access_token
        self._http = http
        self._owns_http = http is None
        self._pacer = pacer or _limiter

    @classmethod
    def from_db(cls, **kwargs) -> "AniListClient":
        record = tokens.get(tokens.ANILIST)
        if not record.access_token:
            raise AniListAuthError("AniList is not connected yet")
        return cls(record.access_token, **kwargs)

    async def __aenter__(self) -> "AniListClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- transport ---------------------------------------------------------
    async def _post(self, document: str, variables: dict[str, Any]) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT)
            self._owns_http = True

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": config.USER_AGENT,
        }
        payload = {"query": document, "variables": variables}
        last_error: str = "no attempt was made"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._pacer.acquire()
            try:
                response = await self._http.post(
                    queries.ENDPOINT, json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(min(2 ** attempt, 20))
                continue

            self._pacer.observe(response.headers)

            if response.status_code == 429:
                delay = self._pacer.penalise(response.headers)
                last_error = f"rate limited, retry after {delay:.0f}s"
                if attempt == MAX_ATTEMPTS:
                    break
                continue

            if response.status_code in (401, 403):
                tokens.mark_broken(
                    tokens.ANILIST,
                    f"AniList rejected the token (HTTP {response.status_code}). "
                    "Re-authorise on the dashboard.",
                )
                raise AniListAuthError(
                    f"AniList rejected the token (HTTP {response.status_code})"
                )

            if response.status_code >= 500:
                last_error = f"AniList returned HTTP {response.status_code}"
                if attempt == MAX_ATTEMPTS:
                    break
                await asyncio.sleep(min(2 ** attempt, 20))
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise AniListError(f"AniList returned non-JSON: {exc}") from exc

            errors = body.get("errors") or []
            if errors and body.get("data") in (None, {}):
                message = "; ".join(
                    str(e.get("message", e)) for e in errors if isinstance(e, dict)
                ) or str(errors)
                if response.status_code == 404 or "not found" in message.lower():
                    return {}
                raise AniListError(f"AniList error: {message}")

            return body.get("data") or {}

        raise AniListError(f"AniList request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- operations --------------------------------------------------------
    async def viewer(self) -> dict[str, Any]:
        data = await self._post(queries.VIEWER, {})
        viewer = data.get("Viewer") or {}
        if not viewer:
            raise AniListAuthError("AniList did not return a viewer for this token")
        return viewer

    async def media_with_entry(self, anilist_id: int) -> tuple[AniListEntryState, dict[str, Any]]:
        """Fetch a title's metadata and this user's list entry in one request."""
        data = await self._post(queries.MEDIA_WITH_ENTRY, {"mediaId": anilist_id})
        media = data.get("Media") or {}
        if not media:
            raise AniListError(f"AniList has no anime with id {anilist_id}")

        title = media.get("title") or {}
        entry = media.get("mediaListEntry") or None
        state = AniListEntryState(
            anilist_id=anilist_id,
            exists=entry is not None,
            list_entry_id=(entry or {}).get("id"),
            status=(entry or {}).get("status"),
            progress=int((entry or {}).get("progress") or 0),
            repeat=int((entry or {}).get("repeat") or 0),
            total_episodes=media.get("episodes"),
            media_status=media.get("status"),
            title=title.get("english") or title.get("romaji"),
            started_at=_clean_fuzzy((entry or {}).get("startedAt")),
            completed_at=_clean_fuzzy((entry or {}).get("completedAt")),
        )
        return state, media

    async def search(self, title: str, per_page: int = 10) -> list[dict[str, Any]]:
        data = await self._post(queries.SEARCH, {"search": title, "perPage": per_page})
        page = data.get("Page") or {}
        return list(page.get("media") or [])

    async def save_entry(self, variables: dict[str, Any]) -> dict[str, Any]:
        """Write a list entry. Variables are absolute values, never deltas."""
        forbidden = {"score", "scoreRaw", "notes", "advancedScores"}
        leaked = forbidden & set(variables)
        if leaked:
            # Belt and braces: the mutation does not even declare these.
            raise AniListError(f"refusing to write {sorted(leaked)} to AniList")
        data = await self._post(queries.SAVE_ENTRY, variables)
        return data.get("SaveMediaListEntry") or {}

    async def recent_list_changes(
        self, user_id: int, *, page: int = 1, per_page: int = 50
    ) -> tuple[list[dict[str, Any]], bool]:
        data = await self._post(
            queries.RECENT_LIST_CHANGES,
            {"userId": user_id, "page": page, "perPage": per_page},
        )
        block = data.get("Page") or {}
        info = block.get("pageInfo") or {}
        return list(block.get("mediaList") or []), bool(info.get("hasNextPage"))


def _clean_fuzzy(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """AniList returns {year: null, month: null, day: null} for an unset date."""
    if not value:
        return None
    if all(value.get(k) is None for k in ("year", "month", "day")):
        return None
    return value
