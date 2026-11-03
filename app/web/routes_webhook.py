"""The Plex webhook endpoint.

Two things are deliberate about the order of this handler:

* The **secret path token** is checked first and a mismatch returns 404, so the
  endpoint does not advertise itself. Plex cannot send an auth header with a
  webhook, which is why the secret is in the path.
* The **account filter** runs before anything else reads the payload, for
  every event type. On a shared server, an event from another user is dropped
  here: no enrichment, no resolution, and nothing about what they watched is
  logged -- only a counter moves. That is the whole point, so it is also what
  the tests assert.

Any event from the configured account marks the webhook as working
(``last_webhook``); only ``media.scrobble`` goes on to be synced.

Plex posts ``multipart/form-data`` with the JSON in a ``payload`` part; plain
JSON bodies are accepted too, which is what makes the endpoint easy to exercise
by hand.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app import auth, db, repo
from app.anilist.client import AniListAuthError, AniListClient
from app.models import WatchEvent
from app.plex import client as plex_client
from app.plex import webhook as plex_webhook
from app.sync.pipeline import handle_watch_event

log = logging.getLogger(__name__)

router = APIRouter()

COUNTER_FOREIGN = "foreign_event_dropped"
COUNTER_ACCEPTED = "webhook_accepted"
COUNTER_IGNORED = "webhook_ignored_event_type"


async def _read_payload(request: Request) -> dict[str, Any] | None:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return None
        return body if isinstance(body, dict) else None

    try:
        form = await request.form()
    except Exception:  # noqa: BLE001
        return None
    raw = form.get("payload")
    if raw is None:
        return None
    try:
        body = json.loads(raw if isinstance(raw, str) else await raw.read())
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


@router.post("/webhook/plex/{path_token}")
async def plex_webhook_endpoint(
    path_token: str, request: Request, background: BackgroundTasks
) -> JSONResponse:
    if not auth.webhook_token_matches(path_token):
        return JSONResponse({"detail": "not found"}, status_code=404)

    payload = await _read_payload(request)
    if payload is None:
        return JSONResponse({"detail": "could not parse payload"}, status_code=400)

    # --- the account filter, before anything else ------------------------
    # It runs for every event type, so another user's play/pause/stop leaves
    # exactly what their scrobble does: one counter, nothing else.
    configured = plex_client.configured_account_id()
    if not plex_webhook.is_own_account(payload, configured):
        db.bump_counter(COUNTER_FOREIGN)
        # Note what is NOT logged: no title, no show, no episode, no account
        # name. Another user's viewing is not this application's business.
        log.info("event dropped: not the configured account")
        return JSONResponse({"status": "ignored", "reason": "other account"})

    # Any delivery from the configured account proves the webhook works,
    # whatever the event. Plex sends media.scrobble only once ~90% of an item
    # has been watched, so waiting for one would leave the setup step stuck
    # after a quick play and stop.
    db.execute(
        "INSERT INTO watermark (name, value) VALUES ('last_webhook', datetime('now'))"
        " ON CONFLICT (name) DO UPDATE SET value = excluded.value"
    )

    event_type = plex_webhook.event_of(payload)
    if event_type != plex_webhook.SCROBBLE:
        db.bump_counter(COUNTER_IGNORED)
        return JSONResponse({"status": "ignored", "event": event_type})

    parsed = plex_webhook.parse(payload)
    if parsed.media_kind not in ("episode", "movie"):
        db.bump_counter(COUNTER_IGNORED)
        return JSONResponse({"status": "ignored", "reason": f"media type {parsed.media_kind}"})

    db.bump_counter(COUNTER_ACCEPTED)

    # Answer Plex immediately; do the work after the response.
    background.add_task(process_scrobble, parsed)
    return JSONResponse({"status": "accepted", "item": parsed.title or parsed.rating_key})


async def process_scrobble(parsed: plex_webhook.ParsedWebhook) -> None:
    """Enrich and run one scrobble through the pipeline."""
    received = datetime.now(UTC)

    show = None
    season = parsed.season
    episode = parsed.episode
    media_kind = parsed.media_kind
    show_rating_key = parsed.show_rating_key

    plex = None
    try:
        plex = plex_client.PlexClient.from_db()
    except plex_client.PlexUnavailable as exc:
        log.error("cannot enrich event: Plex unavailable", extra={"error": str(exc)})

    if plex is not None and parsed.rating_key:
        try:
            context = await plex_client.run(
                plex.episode_context_sync,
                rating_key=parsed.rating_key,
                show_rating_key=parsed.show_rating_key,
            )
            show = context.show
            season = context.season if context.season is not None else season
            episode = context.episode if context.episode is not None else episode
            media_kind = context.media_kind
            show_rating_key = context.show_rating_key or show_rating_key
        except Exception as exc:  # noqa: BLE001
            log.error(
                "enrichment failed",
                extra={"rating_key": parsed.rating_key, "error": str(exc)},
            )

    if show is None:
        # Without the show's external ids nothing can be matched by id. Record
        # it so the review queue explains exactly what was missing.
        from app.models import ShowIds

        show = ShowIds(title=parsed.show_title)

    event = WatchEvent(
        source="webhook",
        received_at=received,
        show=show,
        media_kind=media_kind,
        season=season,
        episode=episode,
        plex_account_id=parsed.account_id,
        rating_key=parsed.rating_key,
        show_rating_key=show_rating_key,
    )

    anilist: AniListClient | None = None
    try:
        anilist = AniListClient.from_db()
    except AniListAuthError:
        anilist = None

    try:
        result = await handle_watch_event(event, plex=plex, anilist=anilist)
        log.info(
            "webhook event handled",
            extra={
                "event_id": result.event_id,
                "status": result.status,
                "label": event.label(),
            },
        )
    finally:
        if anilist is not None:
            await anilist.__aexit__(None, None, None)


@router.get("/webhook/plex/{path_token}")
async def plex_webhook_probe(path_token: str) -> JSONResponse:
    """A GET on the webhook URL, so the setup page can prove the path works."""
    if not auth.webhook_token_matches(path_token):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return JSONResponse(
        {
            "status": "ready",
            "detail": "This is the Plex webhook endpoint. Plex will POST here.",
            "accepted_events": [plex_webhook.SCROBBLE],
            "configured_account_id": plex_client.configured_account_id(),
            "events_recorded": repo.event_count(),
            "foreign_events_dropped": db.get_counter(COUNTER_FOREIGN),
        }
    )
