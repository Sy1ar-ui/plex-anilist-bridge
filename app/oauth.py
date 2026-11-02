"""Connecting AniList and MyAnimeList through their OAuth sign-in pages.

Both providers need an API client that *the user* registers (a shared client
id cannot be shipped in a public repository), with a redirect URL pointing
back at this bridge. The setup wizard shows that exact URL to copy.

* **AniList** uses the authorization-code grant. Its tokens last a year and
  cannot be refreshed, so expiry means signing in again.
* **MyAnimeList** uses the authorization-code grant with PKCE. MAL only
  supports the ``plain`` challenge method, so the verifier is the challenge;
  it never leaves the server except in the token request. Its tokens last
  about a month and come with a refresh token.

Each sign-in gets a random ``state`` that is checked once and then thrown
away, so a callback cannot be replayed or forged from another site. The state
expires after :data:`STATE_TTL_SECONDS`.

Every network function takes an optional ``httpx.AsyncClient`` so tests can
answer for the providers.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app import db, settings_store, tokens

log = logging.getLogger(__name__)

ANILIST_AUTHORIZE = "https://anilist.co/api/v2/oauth/authorize"
ANILIST_TOKEN = "https://anilist.co/api/v2/oauth/token"
ANILIST_DEVELOPER = "https://anilist.co/settings/developer"

MAL_AUTHORIZE = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN = "https://myanimelist.net/v1/oauth2/token"
MAL_ME = "https://api.myanimelist.net/v2/users/@me"
MAL_DEVELOPER = "https://myanimelist.net/apiconfig"

CALLBACK_PATH = {tokens.ANILIST: "/setup/anilist/callback", tokens.MAL: "/setup/mal/callback"}
STATE_TTL_SECONDS = 15 * 60
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_PENDING_KEY = "oauth_pending"


class OAuthError(RuntimeError):
    """The sign-in could not be completed; the message says why, for the user."""


@dataclass(frozen=True)
class ClientCredentials:
    provider: str
    client_id: str
    client_secret: str | None


@dataclass(frozen=True)
class TokenGrant:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


# --------------------------------------------------------------------------
# the user's registered API clients
# --------------------------------------------------------------------------
def get_client(provider: str) -> ClientCredentials | None:
    row = db.query_one(
        "SELECT client_id, client_secret FROM oauth_client WHERE provider = ?", (provider,)
    )
    if not row or not row["client_id"]:
        return None
    return ClientCredentials(provider, row["client_id"], row["client_secret"] or None)


def save_client(provider: str, client_id: str, client_secret: str | None) -> ClientCredentials:
    client_id = client_id.strip()
    if not client_id:
        raise OAuthError("a client id is required")
    if provider == tokens.ANILIST and not client_id.isdigit():
        raise OAuthError("an AniList client id is a number, shown as \"ID\" on the developer page")
    secret = (client_secret or "").strip()
    existing = get_client(provider)
    if not secret and existing and existing.client_id == client_id:
        secret = existing.client_secret or ""   # blank means "unchanged"
    if provider == tokens.ANILIST and not secret:
        raise OAuthError("AniList needs the client secret as well")
    db.execute(
        "INSERT INTO oauth_client (provider, client_id, client_secret) VALUES (?, ?, ?)"
        " ON CONFLICT (provider) DO UPDATE SET client_id = excluded.client_id,"
        "   client_secret = excluded.client_secret",
        (provider, client_id, secret or None),
    )
    return ClientCredentials(provider, client_id, secret or None)


def redirect_uri(base_url: str, provider: str) -> str:
    return base_url.rstrip("/") + CALLBACK_PATH[provider]


# --------------------------------------------------------------------------
# state: one pending sign-in per provider, single use
# --------------------------------------------------------------------------
def _pending() -> dict[str, Any]:
    value = settings_store.get(_PENDING_KEY, None)
    return value if isinstance(value, dict) else {}


def _begin(provider: str, redirect: str, *, verifier: str | None = None) -> str:
    state = secrets.token_urlsafe(24)
    pending = _pending()
    pending[provider] = {
        "state": state,
        "redirect_uri": redirect,
        "verifier": verifier,
        "created": time.time(),
    }
    settings_store.set(_PENDING_KEY, pending)
    return state


def _consume(provider: str, state: str | None) -> dict[str, Any]:
    """Take the pending sign-in for this state, or refuse. It cannot be used twice."""
    pending = _pending()
    entry = pending.pop(provider, None)
    settings_store.set(_PENDING_KEY, pending)
    if not entry or not state or not secrets.compare_digest(str(entry.get("state")), str(state)):
        raise OAuthError(
            "this sign-in did not start here, or was already used; start it again from setup"
        )
    if time.time() - float(entry.get("created") or 0) > STATE_TTL_SECONDS:
        raise OAuthError("the sign-in took too long and expired; start it again")
    return entry


# --------------------------------------------------------------------------
# AniList
# --------------------------------------------------------------------------
def anilist_authorize_url(client: ClientCredentials, redirect: str) -> str:
    state = _begin(tokens.ANILIST, redirect)
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "state": state,
        }
    )
    return f"{ANILIST_AUTHORIZE}?{query}"


async def anilist_finish(
    *, code: str | None, state: str | None, http: httpx.AsyncClient | None = None
) -> TokenGrant:
    entry = _consume(tokens.ANILIST, state)
    client = get_client(tokens.ANILIST)
    if client is None or not client.client_secret:
        raise OAuthError("the AniList client id and secret are missing; enter them again")
    if not code:
        raise OAuthError("AniList did not send an authorisation code back")
    data = await _post_token(
        ANILIST_TOKEN,
        http=http,
        json={
            "grant_type": "authorization_code",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": entry["redirect_uri"],
            "code": code,
        },
        provider="AniList",
    )
    return _grant(data)


# --------------------------------------------------------------------------
# MyAnimeList
# --------------------------------------------------------------------------
def mal_authorize_url(client: ClientCredentials, redirect: str) -> str:
    # 43-128 characters from the unreserved set, as RFC 7636 requires.
    verifier = secrets.token_urlsafe(64)[:128]
    state = _begin(tokens.MAL, redirect, verifier=verifier)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client.client_id,
            "state": state,
            "redirect_uri": redirect,
            "code_challenge": verifier,
            "code_challenge_method": "plain",
        }
    )
    return f"{MAL_AUTHORIZE}?{query}"


async def mal_finish(
    *, code: str | None, state: str | None, http: httpx.AsyncClient | None = None
) -> TokenGrant:
    entry = _consume(tokens.MAL, state)
    client = get_client(tokens.MAL)
    if client is None:
        raise OAuthError("the MyAnimeList client id is missing; enter it again")
    if not code:
        raise OAuthError("MyAnimeList did not send an authorisation code back")
    form = {
        "client_id": client.client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": entry["redirect_uri"],
        "code_verifier": entry.get("verifier") or "",
    }
    if client.client_secret:
        form["client_secret"] = client.client_secret
    data = await _post_token(MAL_TOKEN, http=http, data=form, provider="MyAnimeList")
    return _grant(data)


async def mal_viewer(access_token: str, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    owned = http is None
    client = http or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.get(MAL_ME, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise OAuthError(f"MyAnimeList could not be reached: {exc}") from exc
    finally:
        if owned:
            await client.aclose()
    if response.status_code != 200:
        raise OAuthError(f"MyAnimeList did not accept the new token (HTTP {response.status_code})")
    return response.json()


def store_mal(grant: TokenGrant, viewer: dict[str, Any]) -> None:
    tokens.save(
        tokens.MAL,
        access_token=grant.access_token,
        refresh_token=grant.refresh_token,
        expires_at=grant.expires_at,
        account_name=viewer.get("name"),
        account_id=viewer.get("id"),
        status=tokens.STATUS_OK,
        last_error=None,
    )


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------
async def _post_token(
    url: str, *, http: httpx.AsyncClient | None, provider: str, **kw
) -> dict[str, Any]:
    owned = http is None
    client = http or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.post(url, headers={"Accept": "application/json"}, **kw)
    except httpx.HTTPError as exc:
        raise OAuthError(f"{provider} could not be reached: {exc}") from exc
    finally:
        if owned:
            await client.aclose()
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("access_token"):
        reason = data.get("hint") or data.get("message") or data.get("error_description") \
            or data.get("error") or f"HTTP {response.status_code}"
        raise OAuthError(
            f"{provider} refused the sign-in: {reason}. Check the client id, secret and "
            "that the redirect URL registered with them matches exactly"
        )
    return data


def _grant(data: dict[str, Any]) -> TokenGrant:
    expires_at = None
    try:
        seconds = int(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        expires_at = datetime.now(UTC) + timedelta(seconds=seconds)
    return TokenGrant(
        access_token=str(data["access_token"]),
        refresh_token=data.get("refresh_token") or None,
        expires_at=expires_at,
    )
