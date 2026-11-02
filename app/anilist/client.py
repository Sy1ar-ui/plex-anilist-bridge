"""AniList GraphQL client: rate-limited, retrying, and honest about failures.

Failures are sorted into three kinds, because each needs a different response:

* **auth** (:class:`AniListAuthError`) -- the token was rejected. Retrying is
  pointless until the user re-authorises; the token is marked broken so the
  dashboard says so.
* **unavailable** (:class:`AniListUnavailable`) -- AniList is down, erroring,
  serving a maintenance page, or throttling us for longer than the caller will
  wait (:class:`AniListThrottled`). The work is fine; it just has to wait.
  The pipeline defers the event and the retry job picks it up later.
* everything else (:class:`AniListError`) -- a real problem with the request.

Outage-type failures feed the shared :mod:`app.anilist.health` circuit
breaker, so once AniList is clearly down every caller fails fast instead of
each one sleeping through its own retries.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app import config, tokens
from app.anilist import queries
from app.anilist.health import AniListHealth
from app.anilist.health import health as _shared_health
from app.models import AniListEntryState
from app.ratelimit import RateLimiter, RateLimitWaitTooLong

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_ATTEMPTS = 4

# How long a background request will queue behind AniList's rate limit before
# giving up and deferring. Interactive requests (the settings page) pass a much
# smaller value so a page never hangs for minutes.
DEFAULT_MAX_WAIT_SECONDS = 120.0

# Phrases AniList (or the CDN in front of it) uses when the API itself is off,
# as opposed to the token being bad.
_OUTAGE_PHRASES = (
    "temporarily disabled",
    "maintenance",
    "service unavailable",
    "try again later",
)


class AniListError(RuntimeError):
    """A request to AniList failed in a way the caller should surface."""


class AniListAuthError(AniListError):
    """The token was rejected. Needs a re-auth, not a retry."""


class AniListUnavailable(AniListError):
    """AniList is down or overloaded. The work is fine; try again later.

    ``retry_at`` is a unix timestamp for the earliest sensible retry, when one
    is known (from the circuit breaker or a 429).
    """

    def __init__(self, message: str, *, retry_at: float | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class AniListThrottled(AniListUnavailable):
    """AniList's rate limit means waiting longer than this caller is willing to."""


# One limiter per process so every caller shares the same budget. AniList is
# currently capped at 30/min; the limiter adopts whatever the headers report.
_limiter = RateLimiter(30, name="anilist")


def limiter() -> RateLimiter:
    return _limiter


def health() -> AniListHealth:
    return _shared_health()


class AniListClient:
    def __init__(
        self,
        access_token: str,
        *,
        http: httpx.AsyncClient | None = None,
        pacer: RateLimiter | None = None,
        health_tracker: AniListHealth | None = None,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        respect_circuit: bool = True,
        record_auth_failures: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = access_token
        self._http = http
        self._owns_http = http is None
        self._pacer = pacer or _limiter
        self._health = health_tracker or _shared_health()
        self._max_wait = max_wait
        # Interactive callers (a button press) use fewer attempts, so the page
        # answers in seconds rather than sitting through the backoff sleeps.
        self._max_attempts = max(1, max_attempts)
        # False for an explicit "check now" probe, which must be allowed
        # through an open circuit -- that is how a user tests recovery.
        self._respect_circuit = respect_circuit
        # False when verifying a token that is not stored yet: a typo in a
        # new token must not mark the working, stored one as broken.
        self._record_auth_failures = record_auth_failures
        self._sleep = sleep

    @classmethod
    def from_db(cls, **kwargs) -> "AniListClient":
        record = tokens.get(tokens.ANILIST)
        if not record.access_token:
            raise AniListAuthError("AniList is not connected yet")
        if "max_wait" not in kwargs:
            from app import settings_store

            try:
                kwargs["max_wait"] = float(
                    settings_store.get("anilist_max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS)
                )
            except (TypeError, ValueError):
                kwargs["max_wait"] = DEFAULT_MAX_WAIT_SECONDS
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
    def _auth_failed(self, message: str) -> AniListAuthError:
        if self._record_auth_failures:
            tokens.mark_broken(tokens.ANILIST, f"{message}. Re-authorise on the settings page.")
        return AniListAuthError(message)

    def _unavailable(self, message: str) -> AniListUnavailable:
        return AniListUnavailable(message, retry_at=self._health.retry_at())

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

        for attempt in range(1, self._max_attempts + 1):
            if self._respect_circuit:
                blocked = self._health.blocked_reason()
                if blocked:
                    raise self._unavailable(blocked)

            try:
                await self._pacer.acquire(max_wait=self._max_wait)
            except RateLimitWaitTooLong as exc:
                raise AniListThrottled(
                    f"AniList rate limit: {exc}",
                    retry_at=self._health.retry_at(),
                ) from exc

            try:
                response = await self._http.post(
                    queries.ENDPOINT, json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                last_error = f"could not reach AniList ({type(exc).__name__}: {exc})"
                self._health.record_failure(last_error)
                if attempt == self._max_attempts or self._should_stop():
                    break
                await self._sleep(min(2 ** attempt, 20))
                continue

            self._pacer.observe(response.headers)

            if response.status_code == 429:
                delay = self._pacer.penalise(response.headers)
                self._health.record_throttle(delay)
                last_error = f"rate limited by AniList, retry after {delay:.0f}s"
                if delay > self._max_wait:
                    raise AniListThrottled(
                        f"AniList is rate limiting this client for {delay:.0f}s, "
                        f"longer than the {self._max_wait:.0f}s this request will wait",
                        retry_at=self._health.retry_at(),
                    )
                if attempt == self._max_attempts:
                    break
                continue

            body, body_text = _decode(response)
            message = _error_message(body) if body is not None else body_text[:200]

            if response.status_code in (401, 403):
                if response.status_code == 403 and _looks_like_outage(body, message):
                    # Cloudflare challenge pages and "API temporarily disabled"
                    # come back as 403s. That is AniList being unwell, not the
                    # token being wrong, so do not mark the token broken.
                    last_error = f"AniList refused the request (HTTP 403: {message or 'no detail'})"
                    self._health.record_failure(last_error)
                    if attempt == self._max_attempts or self._should_stop():
                        break
                    await self._sleep(min(2 ** attempt, 20))
                    continue
                raise self._auth_failed(
                    f"AniList rejected the token (HTTP {response.status_code})"
                )

            if response.status_code >= 500 or body is None:
                # A 5xx, or a 200 whose body is an HTML error page.
                last_error = (
                    f"AniList returned HTTP {response.status_code}"
                    if response.status_code >= 500
                    else f"AniList returned a non-JSON response (HTTP {response.status_code})"
                )
                self._health.record_failure(last_error)
                if attempt == self._max_attempts or self._should_stop():
                    break
                await self._sleep(min(2 ** attempt, 20))
                continue

            errors = body.get("errors") or []
            if errors and body.get("data") in (None, {}):
                statuses = _error_statuses(errors)
                lowered = message.lower()
                if "invalid token" in lowered or 401 in statuses:
                    raise self._auth_failed("AniList rejected the token (invalid token)")
                if 429 in statuses:
                    delay = self._pacer.penalise(response.headers)
                    self._health.record_throttle(delay)
                    raise AniListThrottled(
                        f"AniList is rate limiting this client: {message}",
                        retry_at=self._health.retry_at(),
                    )
                if any(s >= 500 for s in statuses) or _looks_like_outage(body, message):
                    last_error = f"AniList error: {message}"
                    self._health.record_failure(last_error)
                    if attempt == self._max_attempts or self._should_stop():
                        break
                    await self._sleep(min(2 ** attempt, 20))
                    continue
                # A well-formed answer about the request itself: AniList is up.
                self._health.record_success()
                if response.status_code == 404 or "not found" in lowered:
                    return {}
                raise AniListError(f"AniList error: {message}")

            self._health.record_success()
            return body.get("data") or {}

        raise self._unavailable(
            f"AniList is unavailable after {attempt} attempt(s): {last_error}"
        )

    def _should_stop(self) -> bool:
        """Stop retrying early once the circuit has opened mid-request."""
        return self._respect_circuit and self._health.circuit_open()

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


def _decode(response: httpx.Response) -> tuple[dict[str, Any] | None, str]:
    """The JSON body if there is one, and the raw text either way."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - undecodable bytes are just "not JSON"
        text = ""
    try:
        body = response.json()
    except ValueError:
        return None, text
    return (body if isinstance(body, dict) else None), text


def _error_message(body: dict[str, Any]) -> str:
    errors = body.get("errors") or []
    if not isinstance(errors, list):
        return str(errors)
    return "; ".join(
        str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
    )


def _error_statuses(errors: list[Any]) -> set[int]:
    statuses: set[int] = set()
    for error in errors:
        if isinstance(error, dict):
            try:
                statuses.add(int(error.get("status")))
            except (TypeError, ValueError):
                continue
    return statuses


def _looks_like_outage(body: dict[str, Any] | None, message: str) -> bool:
    if body is None:
        return True  # an HTML page where JSON should be
    lowered = (message or "").lower()
    return any(phrase in lowered for phrase in _OUTAGE_PHRASES)


def _clean_fuzzy(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """AniList returns {year: null, month: null, day: null} for an unset date."""
    if not value:
        return None
    if all(value.get(k) is None for k in ("year", "month", "day")):
        return None
    return value
