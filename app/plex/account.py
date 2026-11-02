"""Signing in with Plex (the PIN flow) and finding the user's servers.

This replaces pasting an ``X-Plex-Token``:

1. :func:`create_pin` asks plex.tv for a PIN tied to this install's client id.
2. The browser is sent to :func:`auth_url`, where the user signs in to Plex and
   is forwarded back to the bridge.
3. :func:`check_pin` asks plex.tv whether that PIN now carries a token.
4. :func:`servers` lists the servers the account can reach, each with its own
   access token and every address Plex knows for it.
5. :func:`probe` tries those addresses *from this container*, because the
   address that works from the user's browser is not necessarily the one that
   works from a Docker network.

The plex.tv account token is only needed for steps 3-5. The bridge keeps the
chosen server's token, not the account token.

Every function takes an optional ``httpx.AsyncClient`` so tests can answer
for plex.tv without a network.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app import config, settings_store

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
AUTH_APP = "https://app.plex.tv/auth"
PRODUCT = "Plex AniList Bridge"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
PROBE_TIMEOUT = 4.0


class PlexAuthError(RuntimeError):
    """plex.tv refused, expired the PIN, or could not be reached."""


@dataclass(frozen=True)
class Pin:
    id: int
    code: str


@dataclass(frozen=True)
class PlexUser:
    id: int
    username: str
    title: str


@dataclass
class Connection:
    uri: str
    local: bool
    relay: bool
    # Filled in by probe():
    ok: bool | None = None
    error: str | None = None
    latency_ms: int | None = None

    @property
    def label(self) -> str:
        if self.relay:
            return "relay (slow, through Plex)"
        return "local network" if self.local else "remote"


@dataclass
class Server:
    name: str
    client_id: str
    access_token: str
    owned: bool
    connections: list[Connection] = field(default_factory=list)

    @property
    def best(self) -> Connection | None:
        """The first working address, preferring local, then direct, then relay."""
        working = [c for c in self.connections if c.ok]
        return working[0] if working else None


def client_identifier() -> str:
    """This install's stable Plex client id. Plex shows it under Authorized Devices."""
    value = settings_store.get("plex_client_identifier", "")
    if not value:
        value = f"plex-anilist-bridge-{uuid.uuid4()}"
        settings_store.set("plex_client_identifier", value)
    return value


def headers(token: str | None = None) -> dict[str, str]:
    out = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": config.APP_VERSION,
        "X-Plex-Client-Identifier": client_identifier(),
        "X-Plex-Device-Name": PRODUCT,
        "X-Plex-Platform": "Docker",
    }
    if token:
        out["X-Plex-Token"] = token
    return out


async def _request(
    method: str, url: str, *, http: httpx.AsyncClient | None, token: str | None = None, **kw
) -> httpx.Response:
    owned = http is None
    client = http or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        response = await client.request(method, url, headers=headers(token), **kw)
    except httpx.HTTPError as exc:
        raise PlexAuthError(f"plex.tv could not be reached: {exc}") from exc
    finally:
        if owned:
            await client.aclose()
    if response.status_code == 401:
        raise PlexAuthError("plex.tv rejected the sign-in; start again")
    if response.status_code >= 400:
        raise PlexAuthError(f"plex.tv answered HTTP {response.status_code}")
    return response


# --------------------------------------------------------------------------
# the PIN
# --------------------------------------------------------------------------
async def create_pin(*, http: httpx.AsyncClient | None = None) -> Pin:
    response = await _request("POST", f"{PLEX_TV}/api/v2/pins", http=http, params={"strong": "true"})
    data = response.json()
    try:
        return Pin(int(data["id"]), str(data["code"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlexAuthError("plex.tv returned a PIN the bridge could not read") from exc


def auth_url(pin: Pin, forward_url: str) -> str:
    """Where to send the browser to sign in. Plex sends it on to ``forward_url``."""
    query = urlencode(
        {
            "clientID": client_identifier(),
            "code": pin.code,
            "context[device][product]": PRODUCT,
            "forwardUrl": forward_url,
        },
        quote_via=quote,
    )
    # The parameters go in the fragment: that is how app.plex.tv reads them.
    return f"{AUTH_APP}#?{query}"


async def check_pin(pin_id: int, *, http: httpx.AsyncClient | None = None) -> str | None:
    """The account token once the user has signed in; None while still waiting."""
    try:
        response = await _request("GET", f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", http=http)
    except PlexAuthError as exc:
        if "HTTP 404" in str(exc):
            raise PlexAuthError("that sign-in has expired; start again") from exc
        raise
    token = (response.json() or {}).get("authToken")
    return str(token) if token else None


# --------------------------------------------------------------------------
# the account and its servers
# --------------------------------------------------------------------------
async def user(token: str, *, http: httpx.AsyncClient | None = None) -> PlexUser:
    data = (await _request("GET", f"{PLEX_TV}/api/v2/user", http=http, token=token)).json()
    return PlexUser(
        id=int(data.get("id") or 0),
        username=str(data.get("username") or ""),
        title=str(data.get("title") or data.get("username") or ""),
    )


async def servers(token: str, *, http: httpx.AsyncClient | None = None) -> list[Server]:
    response = await _request(
        "GET",
        f"{PLEX_TV}/api/v2/resources",
        http=http,
        token=token,
        params={"includeHttps": "1", "includeRelay": "1", "includeIPv6": "0"},
    )
    found: list[Server] = []
    for item in response.json() or []:
        if "server" not in str(item.get("provides") or "").split(","):
            continue
        connections = _connections(item.get("connections") or [])
        found.append(
            Server(
                name=str(item.get("name") or "Plex Media Server"),
                client_id=str(item.get("clientIdentifier") or ""),
                access_token=str(item.get("accessToken") or token),
                owned=bool(item.get("owned")),
                connections=connections,
            )
        )
    # Your own servers first.
    return sorted(found, key=lambda s: (not s.owned, s.name.lower()))


def _connections(raw: list[dict[str, Any]]) -> list[Connection]:
    """Every address worth trying, local first, relay last, without repeats.

    For a local address Plex lists only its ``plex.direct`` HTTPS name, which
    some routers refuse to resolve (DNS rebinding protection). The plain
    ``http://ip:port`` form is added as a second option for those networks.
    """
    out: list[Connection] = []
    seen: set[str] = set()

    def add(uri: str, local: bool, relay: bool) -> None:
        uri = uri.rstrip("/")
        if uri and uri not in seen:
            seen.add(uri)
            out.append(Connection(uri=uri, local=local, relay=relay))

    for conn in raw:
        local, relay = bool(conn.get("local")), bool(conn.get("relay"))
        add(str(conn.get("uri") or ""), local, relay)
        if local and not relay and conn.get("address") and conn.get("port"):
            add(f"http://{conn['address']}:{conn['port']}", True, False)

    return sorted(out, key=lambda c: (c.relay, not c.local, not c.uri.startswith("https")))


async def probe(
    server: Server, *, http: httpx.AsyncClient | None = None, timeout: float = PROBE_TIMEOUT
) -> Server:
    """Try every address at once, from here. Marks each ok or not, in place."""
    owned = http is None
    client = http or httpx.AsyncClient(timeout=timeout, verify=True)

    async def one(conn: Connection) -> None:
        started = asyncio.get_running_loop().time()
        try:
            response = await client.get(
                f"{conn.uri}/identity",
                headers={"Accept": "application/json", "X-Plex-Token": server.access_token},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            conn.ok, conn.error = False, _short(exc)
            return
        conn.latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        if response.status_code != 200:
            conn.ok, conn.error = False, f"HTTP {response.status_code}"
            return
        identity = _identity(response)
        if server.client_id and identity and identity != server.client_id:
            conn.ok, conn.error = False, "a different Plex server answered at this address"
            return
        conn.ok = True

    try:
        await asyncio.gather(*(one(c) for c in server.connections))
    finally:
        if owned:
            await client.aclose()
    return server


def _identity(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    container = data.get("MediaContainer") if isinstance(data, dict) else None
    return str((container or {}).get("machineIdentifier") or "") or None


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect" + (" (certificate problem)" if "CERTIFICATE" in text.upper() else "")
    return text[:120]


# --------------------------------------------------------------------------
# wizard state: the PIN and the account token, only until a server is chosen
# --------------------------------------------------------------------------
_STATE_KEY = "plex_signin"


def remember_pin(pin: Pin) -> str:
    """Store the PIN with a nonce the forwarded browser must bring back."""
    nonce = secrets.token_urlsafe(16)
    settings_store.set(_STATE_KEY, {"pin_id": pin.id, "nonce": nonce})
    return nonce


def pending() -> dict[str, Any]:
    value = settings_store.get(_STATE_KEY, None)
    return value if isinstance(value, dict) else {}


def remember_token(token: str, account: PlexUser) -> None:
    settings_store.set(
        _STATE_KEY,
        {"token": token, "user": {"id": account.id, "username": account.username, "title": account.title}},
    )


def forget() -> None:
    """Drop the account token once a server is saved; only the server token is kept."""
    from app import db

    db.execute("DELETE FROM setting WHERE key = ?", (_STATE_KEY,))
