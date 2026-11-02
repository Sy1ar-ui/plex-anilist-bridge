"""MyAnimeList API v2: read the user's list, update one entry.

Only three calls are used: one title's list status, the whole list (for the
full-reconcile preview), and ``PATCH my_list_status``. Nothing deletes an
entry, and comments and tags are never sent.

Tokens last about a month. One that is about to expire is refreshed before
use, and a 401 triggers one refresh and one retry; if that fails the token is
marked broken so the UI says so.

MAL publishes no rate limit, so requests are spaced out
(:data:`MIN_INTERVAL_SECONDS`), and a 429, a 5xx or a timeout is reported as
:class:`MALUnavailable` so the caller can stop and pick up next time instead
of hammering it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app import tokens

log = logging.getLogger(__name__)

API = "https://api.myanimelist.net/v2"
MIN_INTERVAL_SECONDS = 0.5
REFRESH_WITHIN = timedelta(days=2)
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
LIST_FIELDS = "list_status{status,score,num_episodes_watched,is_rewatching,num_times_rewatched,start_date,finish_date},num_episodes"

# What is ever sent in a PATCH. Comments, tags, priority and rewatch value are not.
WRITABLE = frozenset(
    {"status", "is_rewatching", "score", "num_watched_episodes", "num_times_rewatched",
     "start_date", "finish_date"}
)


class MALError(RuntimeError):
    pass


class MALAuthError(MALError):
    """The token is missing, or rejected even after a refresh."""


class MALUnavailable(MALError):
    """MAL is down, slow or rate limiting: try again later."""


def normalise(list_status: dict[str, Any] | None) -> dict[str, Any] | None:
    """A MAL list status in the shape the rules compare (and PATCH uses).

    MAL reads ``num_episodes_watched`` but writes ``num_watched_episodes``;
    everything here uses the write name.
    """
    if not list_status:
        return None
    return {
        "status": list_status.get("status"),
        "is_rewatching": bool(list_status.get("is_rewatching")),
        "num_watched_episodes": int(list_status.get("num_episodes_watched") or 0),
        "score": int(list_status.get("score") or 0),
        "num_times_rewatched": int(list_status.get("num_times_rewatched") or 0),
        "start_date": list_status.get("start_date") or None,
        "finish_date": list_status.get("finish_date") or None,
    }


class MALClient:
    def __init__(
        self,
        access_token: str,
        *,
        http: httpx.AsyncClient | None = None,
        refresh: Callable[[], Awaitable[str | None]] | None = None,
        min_interval: float = MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = access_token
        self._http = http
        self._owns_http = http is None
        self._refresh = refresh
        self._min_interval = min_interval
        self._sleep = sleep
        self._last_request = 0.0

    @classmethod
    async def from_db(cls, **kwargs) -> MALClient:
        """A client for the stored token, refreshed first if it is about to expire."""
        record = tokens.get(tokens.MAL)
        if not record.access_token:
            raise MALAuthError("MyAnimeList is not connected")
        if record.status == tokens.STATUS_BROKEN:
            raise MALAuthError("the MyAnimeList token needs re-authorising")
        token = record.access_token
        remaining = record.expires_in()
        if remaining is not None and remaining < REFRESH_WITHIN:
            token = await refresh_stored_token() or token
        kwargs.setdefault("refresh", refresh_stored_token)
        return cls(token, **kwargs)

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- transport ---------------------------------------------------------
    async def _request(self, method: str, path: str, *, retried: bool = False, **kw) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT)
            self._owns_http = True
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            await self._sleep(wait)
        self._last_request = time.monotonic()
        try:
            response = await self._http.request(
                method, f"{API}{path}", headers={"Authorization": f"Bearer {self._token}"}, **kw
            )
        except httpx.TimeoutException as exc:
            raise MALUnavailable("MyAnimeList timed out") from exc
        except httpx.HTTPError as exc:
            raise MALUnavailable(f"MyAnimeList could not be reached: {exc}") from exc

        if response.status_code == 401:
            if not retried and self._refresh is not None:
                new = await self._refresh()
                if new:
                    self._token = new
                    return await self._request(method, path, retried=True, **kw)
            tokens.mark_broken(tokens.MAL, "MyAnimeList rejected the token. Sign in again on the setup page.")
            raise MALAuthError("MyAnimeList rejected the token")
        if response.status_code == 429 or response.status_code >= 500:
            raise MALUnavailable(f"MyAnimeList returned HTTP {response.status_code}")
        if response.status_code == 404:
            raise MALError("MyAnimeList has no such anime")
        if response.status_code >= 400:
            raise MALError(f"MyAnimeList refused the request (HTTP {response.status_code}): {_message(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise MALUnavailable("MyAnimeList returned something that is not JSON") from exc

    # -- operations --------------------------------------------------------
    async def list_status(self, mal_id: int) -> dict[str, Any] | None:
        """This user's entry for one title, normalised; None if it is not on their list."""
        data = await self._request("GET", f"/anime/{int(mal_id)}", params={"fields": "my_list_status,num_episodes"})
        return normalise(data.get("my_list_status"))

    async def animelist(self, *, page_size: int = 1000) -> dict[int, dict[str, Any]]:
        """The whole list, keyed by MAL id."""
        out: dict[int, dict[str, Any]] = {}
        params: dict[str, Any] | None = {"fields": LIST_FIELDS, "limit": page_size, "nsfw": "true"}
        path = "/users/@me/animelist"
        while path:
            data = await self._request("GET", path, params=params)
            for item in data.get("data") or []:
                node = item.get("node") or {}
                if node.get("id"):
                    entry = normalise(item.get("list_status")) or {}
                    entry["title"] = node.get("title")
                    out[int(node["id"])] = entry
            following = (data.get("paging") or {}).get("next")
            if not following:
                break
            path, params = following.removeprefix(API), None
        return out

    async def update(self, mal_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        """PATCH one entry with absolute values. Refuses anything outside WRITABLE."""
        extra = set(fields) - WRITABLE
        if extra:
            raise MALError(f"refusing to write {sorted(extra)} to MyAnimeList")
        form = {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in fields.items()}
        return await self._request("PATCH", f"/anime/{int(mal_id)}/my_list_status", data=form)


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    return str(data.get("message") or data.get("error") or data)[:200]


async def refresh_stored_token(*, http: httpx.AsyncClient | None = None) -> str | None:
    """Swap the stored refresh token for a new access token. None if that fails."""
    from app import oauth

    record = tokens.get(tokens.MAL)
    client = oauth.get_client(tokens.MAL)
    if not record.refresh_token or client is None:
        return None
    form = {"client_id": client.client_id, "grant_type": "refresh_token",
            "refresh_token": record.refresh_token}
    if client.client_secret:
        form["client_secret"] = client.client_secret
    try:
        data = await oauth._post_token(oauth.MAL_TOKEN, http=http, data=form, provider="MyAnimeList")
    except oauth.OAuthError as exc:
        log.warning("MAL token refresh failed", extra={"error": str(exc)})
        return None
    grant = oauth._grant(data)
    tokens.save(
        tokens.MAL,
        access_token=grant.access_token,
        refresh_token=grant.refresh_token,
        expires_at=grant.expires_at or (datetime.now(UTC) + timedelta(days=30)),
        status=tokens.STATUS_OK,
        last_error=None,
    )
    log.info("MAL token refreshed")
    return grant.access_token
