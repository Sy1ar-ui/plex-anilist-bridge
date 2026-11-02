"""Notifications: tell the user when something needs them.

Three things are worth a ping, each with its own switch in settings:

* **review** -- a show could not be matched confidently. One notification per
  show and season, not one per episode.
* **rating** -- a series is finished and waiting for a rating before it is
  completed on AniList.
* **token** -- AniList or MyAnimeList rejected the stored token, so syncing is
  waiting for you to sign in again.

Delivery is a single POST to the configured URL, shaped for Discord, ntfy or
plain JSON (picked from the URL unless set explicitly). It runs in the
background with a short timeout: a slow or broken webhook never delays or
fails a sync. Every send is logged, with its error if it failed.

Nothing here reads another Plex user's activity: notifications are only
raised from events that already passed the account filter.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app import db, settings_store

log = logging.getLogger(__name__)

KIND_REVIEW = "review"
KIND_RATING = "rating"
KIND_TOKEN = "token"
KIND_TEST = "test"

SWITCH = {KIND_REVIEW: "notify_on_review", KIND_RATING: "notify_on_rating", KIND_TOKEN: "notify_on_token_error"}
# How long the same thing stays quiet after it was sent once.
COOLDOWN_HOURS = {KIND_REVIEW: 24 * 7, KIND_RATING: 24 * 365, KIND_TOKEN: 24}

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
APP_NAME = "Plex AniList Bridge"

_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Message:
    kind: str
    title: str
    body: str
    link: str | None = None


# --------------------------------------------------------------------------
# what to send, and whether to
# --------------------------------------------------------------------------
def configured_url() -> str:
    url = str(settings_store.get("notify_webhook_url", "") or "").strip()
    return url if urlparse(url).scheme in ("http", "https") else ""


def service(url: str, chosen: str | None = None) -> str:
    """discord | ntfy | json: the setting if chosen, else a guess from the URL."""
    chosen = chosen or str(settings_store.get("notify_service", "auto") or "auto")
    if chosen in ("discord", "ntfy", "json"):
        return chosen
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith(("discord.com", "discordapp.com")) and "/api/webhooks/" in parsed.path:
        return "discord"
    if "ntfy" in host:
        return "ntfy"
    return "json"


def link(path: str) -> str | None:
    """An absolute link into the UI, from the address it was last opened at."""
    base = str(settings_store.get("ui_base_url", "") or "").rstrip("/")
    return f"{base}{path}" if base else None


def _recently_sent(key: str, hours: int) -> bool:
    row = db.query_one(
        "SELECT 1 FROM notification_log WHERE dedupe_key = ? AND result != 'failed'"
        " AND at >= datetime('now', ?) LIMIT 1",
        (key, f"-{int(hours)} hours"),
    )
    return row is not None


def notify(message: Message, *, key: str | None = None) -> int | None:
    """Queue a notification. Returns its log id, or None if it was not wanted.

    Never raises: a notification must not be able to break the thing that
    raised it.
    """
    try:
        url = configured_url()
        if not url:
            return None
        switch = SWITCH.get(message.kind)
        if switch and not settings_store.get(switch, True):
            return None
        if key and _recently_sent(key, COOLDOWN_HOURS.get(message.kind, 24)):
            return None
        log_id = db.execute(
            "INSERT INTO notification_log (at, kind, dedupe_key, title, body, link, result)"
            " VALUES (datetime('now'), ?, ?, ?, ?, ?, 'pending')",
            (message.kind, key, message.title, message.body, message.link),
        )
        _dispatch(log_id, url, message)
        return log_id
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("could not queue a notification")
        return None


def _dispatch(log_id: int, url: str, message: Message) -> None:
    """Send in the background if there is an event loop, else right away."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(send(log_id, url, message))
        return
    task = loop.create_task(send(log_id, url, message))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for queued sends. For tests and shutdown."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------
def payload(kind: str, message: Message) -> dict[str, Any]:
    """The request for one service: {"json": ...} or {"content": ..., "headers": ...}."""
    text = message.body + (f"\n{message.link}" if message.link and kind != "ntfy" else "")
    if kind == "discord":
        embed: dict[str, Any] = {"title": message.title[:256], "description": message.body[:4000]}
        if message.link:
            embed["url"] = message.link
        return {"json": {"username": APP_NAME, "embeds": [embed], "allowed_mentions": {"parse": []}}}
    if kind == "ntfy":
        headers = {"Title": _header(message.title), "Tags": {KIND_TOKEN: "warning", KIND_RATING: "star",
                                                            KIND_REVIEW: "mag"}.get(message.kind, "bell")}
        if message.link:
            headers["Click"] = message.link
        return {"content": message.body.encode("utf-8"), "headers": headers}
    return {"json": {"source": "plex-anilist-bridge", "kind": message.kind, "title": message.title,
                     "message": message.body, "text": f"{message.title}\n{text}", "url": message.link}}


def _header(value: str) -> str:
    """HTTP headers are latin-1; keep the title readable rather than failing."""
    return value.encode("latin-1", "replace").decode("latin-1")[:250]


async def send(log_id: int, url: str, message: Message, *, http: httpx.AsyncClient | None = None,
               service_name: str | None = None) -> bool:
    owned = http is None
    client = http or httpx.AsyncClient(timeout=TIMEOUT)
    error: str | None = None
    try:
        response = await client.post(url, **payload(service(url, service_name), message))
        if response.status_code >= 400:
            error = f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as exc:
        error = f"could not reach the webhook: {exc or exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001 - logged, never raised
        error = str(exc)
    finally:
        if owned:
            await client.aclose()
    db.execute(
        "UPDATE notification_log SET result = ?, error = ? WHERE id = ?",
        ("failed" if error else "sent", error, log_id),
    )
    if error:
        log.warning("notification failed", extra={"kind": message.kind, "error": error})
    return error is None


def recent(limit: int = 8) -> list[dict[str, Any]]:
    return [dict(r) for r in db.query("SELECT * FROM notification_log ORDER BY id DESC LIMIT ?", (limit,))]


# --------------------------------------------------------------------------
# the three things worth a ping
# --------------------------------------------------------------------------
def review_needed(*, event_id: int, show_title: str | None, season: int | None, media_kind: str,
                  plex_guid: str | None, reason: str) -> None:
    name = show_title or "Something you watched"
    where = name if media_kind == "movie" else f"{name} season {season if season is not None else '?'}"
    notify(
        Message(KIND_REVIEW, f"Needs review: {where}",
                f"The bridge could not match this confidently, so nothing was written. {reason}",
                link(f"/review/{event_id}")),
        key=f"review:{plex_guid or name}:{'movie' if media_kind == 'movie' else season}",
    )


def ready_to_rate(*, pending_id: int, title: str | None, total_episodes: int, completed_on: str) -> None:
    name = title or "A series"
    notify(
        Message(KIND_RATING, f"Ready to rate: {name}",
                f"You finished {name} ({completed_on}). It is held at {total_episodes - 1}/{total_episodes} "
                "on AniList until you rate it.",
                link("/#rate")),
        key=f"rating:{pending_id}",
    )


def token_rejected(provider: str, error: str) -> None:
    name = "AniList" if provider == "anilist" else "MyAnimeList"
    notify(
        Message(KIND_TOKEN, f"{name} needs you to sign in again",
                f"{name} rejected the bridge's token ({error}). Syncing to {name} is paused until you sign in "
                "again; nothing is lost in the meantime.",
                link(f"/setup/{provider}")),
        key=f"token:{provider}",
    )


async def send_test(*, url: str | None = None, service_name: str | None = None) -> tuple[bool, str]:
    """Send one test message now, waiting for the answer. Defaults to the saved URL."""
    url = url if url is not None else configured_url()
    if not url or urlparse(url).scheme not in ("http", "https"):
        return False, "Enter an http or https webhook URL first."
    message = Message(KIND_TEST, f"{APP_NAME}: test notification",
                      "Notifications are working. You will hear about shows to review, series to rate, "
                      "and sign-ins that need renewing.", link("/"))
    log_id = db.execute(
        "INSERT INTO notification_log (at, kind, title, body, link, result)"
        " VALUES (datetime('now'), ?, ?, ?, ?, 'pending')",
        (message.kind, message.title, message.body, message.link),
    )
    ok = await send(log_id, url, message, service_name=service_name)
    if ok:
        return True, f"Test sent as {service(url, service_name)}. Remember to save if you changed the URL."
    row = db.query_one("SELECT error FROM notification_log WHERE id = ?", (log_id,))
    return False, f"The test was not delivered: {row['error'] if row else 'unknown error'}"
