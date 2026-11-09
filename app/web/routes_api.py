"""A small JSON API for a personal agent (OpenClaw, Hermes, a script) acting for you.

Everything needs ``Authorization: Bearer <token>``, with the token generated
under *Settings -> Agent access*. The API is off until a token exists.

It can read what needs you and act on it -- rate and complete a series,
complete it without a rating, stop waiting, link or dismiss a review item --
through the same functions the web UI uses, so dry run, never-decrease and
every other rule apply exactly as they do there. It cannot change settings,
turn dry run off, or read any token.

``GET /api/v1`` describes every endpoint, for an agent to read.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth, config, repo, settings_store
from app.sync import completion, ignored, review

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


def require_token(request: Request) -> None:
    if not auth.api_enabled():
        raise HTTPException(status_code=403, detail="the agent API is off; generate a token in Settings")
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not auth.api_token_valid(token.strip()):
        raise HTTPException(status_code=401, detail="missing or wrong API token",
                            headers={"WWW-Authenticate": "Bearer"})


Auth = Depends(require_token)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        data = None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="the body must be a JSON object")
    return data


ENDPOINTS = [
    ("GET", "/api/v1/status", "Dry run, AniList health, and how many things need you."),
    ("GET", "/api/v1/ratings", "Finished series waiting for a rating before they are completed."),
    ("POST", "/api/v1/ratings/{id}", ('Rate and complete: {"score": 8.5} (1-10). Or {"action": '
                                      '"complete_without_rating"} or {"action": "dont_complete"}. '
                                      "Completion is dated the day the final episode was watched.")),
    ("GET", "/api/v1/review", "Shows that could not be matched confidently, with the candidates found."),
    ("POST", "/api/v1/review/{event_id}/link", ('Link to an AniList title: {"anilist_id": 21, "ep_from": 1, '
                                                '"ep_to": null, "start": 1, "scope": "plex_guid"|"tvdb"}. '
                                                "Plex episodes ep_from..ep_to become AniList episodes from "
                                                "start. Re-runs the covered episodes.")),
    ("POST", "/api/v1/review/{event_id}/dismiss", "Take an item out of the review queue once; writes nothing."),
    ("POST", "/api/v1/review/{event_id}/drop", ("Drop the show behind an item: it leaves the queue and future "
                                                 "episodes are skipped without asking. Reversible; writes nothing.")),
    ("GET", "/api/v1/review/dropped", "Shows that have been dropped."),
    ("DELETE", "/api/v1/review/dropped/{ignored_id}", "Undrop a show; affects episodes watched from then on."),
    ("GET", "/api/v1/activity", "Recent events, newest first. ?limit=20 (max 100), ?status=written."),
]


@router.get("", dependencies=[Auth])
@router.get("/", dependencies=[Auth], include_in_schema=False)
async def index() -> dict[str, Any]:
    return {
        "name": "plex-anilist-bridge",
        "version": config.APP_VERSION,
        "dry_run": settings_store.is_dry_run(),
        "endpoints": [{"method": m, "path": p, "does": d} for m, p, d in ENDPOINTS],
        "notes": "Every write goes through the same rules as the web UI. While dry run is on, nothing "
                 "is sent to AniList or MyAnimeList; results say what would have been written.",
    }


@router.get("/status", dependencies=[Auth])
async def status() -> dict[str, Any]:
    from app.web.routes_ui import anilist_status

    snap = anilist_status()
    return {
        "version": config.APP_VERSION,
        "dry_run": settings_store.is_dry_run(),
        "anilist": {"state": snap.get("state"), "summary": snap.get("summary"),
                    "token_rejected": bool(snap.get("token_broken"))},
        "waiting": {
            "to_rate": completion.open_count(),
            "to_review": review.open_count(),
            "deferred": snap.get("deferred", 0),
        },
    }


# --------------------------------------------------------------------------
# ratings
# --------------------------------------------------------------------------
def _rating(item: dict[str, Any]) -> dict[str, Any]:
    total = int(item["total_episodes"])
    return {
        "id": item["id"],
        "title": item.get("display_title") or item.get("title"),
        "anilist_id": item["anilist_id"],
        "episodes": total,
        "held_at": f"{total - 1}/{total}",
        "finished_on": item["completed_on"],
        "status": "waiting_for_anilist" if item["status"] == completion.QUEUED else "waiting_for_you",
        "score": item.get("score"),
        "last_error": item.get("last_error"),
    }


@router.get("/ratings", dependencies=[Auth])
async def ratings() -> dict[str, Any]:
    return {"ratings": [_rating(i) for i in completion.list_open()]}


@router.post("/ratings/{pending_id}", dependencies=[Auth])
async def rate(pending_id: int, request: Request) -> JSONResponse:
    body = await _body(request)
    action = str(body.get("action") or ("rate" if "score" in body else "")).strip()
    if action == "dont_complete":
        outcome = completion.dismiss(pending_id)
    elif action in ("rate", "complete_without_rating"):
        score = None
        if action == "rate":
            try:
                score = completion.parse_score(str(body.get("score")))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        outcome = await completion.submit(pending_id, score)
    else:
        raise HTTPException(status_code=422, detail='send {"score": 1-10}, or an "action" of '
                                                    '"complete_without_rating" or "dont_complete"')
    if outcome.status == "missing":
        raise HTTPException(status_code=404, detail=outcome.detail)
    log.info("rating via API", extra={"pending_id": pending_id, "action": action, "status": outcome.status})
    return JSONResponse({"ok": outcome.ok, "status": outcome.status, "detail": outcome.detail},
                        status_code=200 if outcome.ok else 409)


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------
def _group(g: review.Group) -> dict[str, Any]:
    return {
        "event_id": g.latest_id,
        "title": g.title,
        "kind": g.media_kind,
        "season": g.season,
        "episodes": g.episodes,
        "status": g.status,
        "why": g.summary,
        "tvdb_id": g.tvdb_id,
        "candidates": [
            {"anilist_id": c.get("anilist_id"), "title": c.get("title"),
             "entry_episode": c.get("entry_episode"), "ep_offset": c.get("ep_offset"),
             "episodes": c.get("total_episodes"), "source": c.get("layer")}
            for c in g.candidates
        ],
    }


@router.get("/review", dependencies=[Auth])
async def review_queue() -> dict[str, Any]:
    return {"review": [_group(g) for g in review.open_groups()]}


def _open_group(event_id: int) -> review.Group:
    group = review.group_for_event(event_id)
    if group is None:
        raise HTTPException(status_code=404, detail="that item is no longer waiting for review")
    return group


@router.post("/review/{event_id}/link", dependencies=[Auth])
async def review_link(event_id: int, request: Request) -> dict[str, Any]:
    from app.web.routes_ui import _rerun

    body = await _body(request)
    group = _open_group(event_id)
    anilist_id = review.parse_anilist_ref(str(body.get("anilist_id") or ""))
    if anilist_id is None:
        raise HTTPException(status_code=422, detail="anilist_id must be an AniList id or anilist.co link")
    try:
        spec = review.parse_range(body.get("ep_from", 1), body.get("ep_to") or "", body.get("start", 1))
        review.save_override(group, anilist_id=anilist_id, spec=spec,
                             scope=str(body.get("scope") or review.SCOPE_SHOW))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    covered = review.covered_events(group, spec)
    rerun, ran = (await _rerun(covered)) if covered else ("no waiting episodes are in that range", False)
    return {"ok": True, "saved": True, "rerun": ran, "detail": rerun, "covered_events": covered}


@router.post("/review/{event_id}/dismiss", dependencies=[Auth])
async def review_dismiss(event_id: int) -> dict[str, Any]:
    group = _open_group(event_id)
    return {"ok": True, "dismissed_events": review.dismiss(group)}


@router.post("/review/{event_id}/drop", dependencies=[Auth])
async def review_drop(event_id: int) -> dict[str, Any]:
    """Drop the show behind this item. Nothing is written to AniList."""
    group = _open_group(event_id)
    if group.plex_guid:
        scope_type, scope_key = ignored.SCOPE_SHOW, group.plex_guid
    elif group.tvdb_id:
        scope_type, scope_key = ignored.SCOPE_TVDB, str(group.tvdb_id)
    else:
        raise HTTPException(status_code=422,
                            detail="that item has no Plex GUID or TVDB id to drop it by")
    ignored.add(scope_type=scope_type, scope_key=scope_key, title=group.title)
    return {"ok": True, "dropped": {"scope": scope_type, "key": scope_key, "title": group.title},
            "dismissed_events": review.dismiss(group)}


@router.get("/review/dropped", dependencies=[Auth])
async def review_dropped() -> dict[str, Any]:
    return {"dropped": ignored.listing()}


@router.delete("/review/dropped/{ignored_id}", dependencies=[Auth])
async def review_undrop(ignored_id: int) -> dict[str, Any]:
    if not ignored.remove(ignored_id):
        raise HTTPException(status_code=404, detail="that show is not on the dropped list")
    return {"ok": True}


# --------------------------------------------------------------------------
# activity
# --------------------------------------------------------------------------
@router.get("/activity", dependencies=[Auth])
async def activity(limit: int = 20, status: str | None = None) -> dict[str, Any]:
    limit = max(1, min(100, limit))
    events = repo.recent_events(limit=limit, status=status or None)
    keep = ("id", "received_at", "viewed_at", "show_title", "season", "episode", "media_kind",
            "status", "confidence", "anilist_id", "entry_episode", "detail")
    return {"events": [{k: e.get(k) for k in keep} for e in events]}
