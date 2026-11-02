"""The MyAnimeList page: mirror status, the full-reconcile preview, recent writes."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import settings_store, tokens
from app.anilist.client import AniListAuthError, AniListClient, AniListError
from app.mal import reconcile as mal_reconcile
from app.mal import sync as mal_sync
from app.mal.client import MALAuthError, MALClient, MALError
from app.web.routes_ui import (
    _base_context,
    _redirect,
    _watermark,
    require_session,
    templates,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/mal", response_class=HTMLResponse)
async def mal_page(request: Request, _: None = Depends(require_session), notice: str | None = None):
    plan = mal_reconcile.draft()
    dry_run = settings_store.is_dry_run()
    context = _base_context(
        request,
        enabled=bool(settings_store.get("mal_sync_enabled", False)),
        blocked=mal_sync.blocked_reason(),
        mal_token=tokens.get(tokens.MAL),
        running=mal_sync.is_running(),
        last_poll=_watermark("mal_last_poll"),
        last_poll_summary=_watermark("mal_last_poll_summary"),
        watermark=mal_sync.get_watermark(dry_run),
        plan=plan,
        plan_items=mal_reconcile.items(plan["id"]) if plan else None,
        log_rows=mal_sync.recent_log(),
        sync_score=bool(settings_store.get("mal_sync_score", True)),
        sync_dates=bool(settings_store.get("mal_sync_dates", False)),
        notice=notice,
    )
    return templates.TemplateResponse(request, "mal.html", context)


@router.post("/mal/poll")
async def mal_poll_now(_: None = Depends(require_session)):
    report = await mal_sync.poll()
    level = "error" if report.detail.startswith(("stopped", "AniList could not")) else "ok"
    if mal_sync.blocked_reason():
        level = "error"
    return _redirect("/mal", report.summary().capitalize() + ".", level)


@router.post("/mal/reconcile")
async def mal_build_preview(_: None = Depends(require_session)):
    """Read both whole lists and store a preview. Writes nothing."""
    try:
        anilist = AniListClient.from_db(max_wait=30.0, max_attempts=2)
    except AniListAuthError as exc:
        return _redirect("/mal", f"Cannot build a preview: {exc}.", "error")
    mal = None
    try:
        mal = await MALClient.from_db()
        plan_id = await mal_reconcile.build(anilist=anilist, mal=mal)
    except (MALAuthError, ValueError) as exc:
        return _redirect("/mal", f"Cannot build a preview: {exc}.", "error")
    except (AniListError, MALError) as exc:
        return _redirect("/mal", f"The preview could not be built: {exc}. Nothing was written.", "error")
    finally:
        await anilist.__aexit__(None, None, None)
        if mal is not None:
            await mal.__aexit__(None, None, None)
    counts = (mal_reconcile.draft() or {}).get("summary", {})
    changes = sum(counts.get(k, 0) for k in mal_reconcile.ACTIONABLE)
    return _redirect(
        "/mal#preview",
        f"Preview ready (plan {plan_id}): {changes} difference(s) to review. Nothing has been written.",
        "ok",
    )


@router.post("/mal/reconcile/{plan_id}/apply")
async def mal_apply(request: Request, plan_id: int, _: None = Depends(require_session)):
    form = await request.form()
    ticked = {int(v) for v in form.getlist("item") if str(v).isdigit()}
    if not ticked:
        return _redirect("/mal#preview", "Nothing was ticked, so nothing was written.")
    try:
        mal = await MALClient.from_db()
    except MALAuthError as exc:
        return _redirect("/mal#preview", f"Not applied: {exc}.", "error")
    try:
        report = await mal_reconcile.apply(plan_id, ticked, mal=mal)
    finally:
        await mal.__aexit__(None, None, None)
    return _redirect("/mal", f"Reconcile: {report.summary()}.", "error" if report.stopped else "ok")


@router.post("/mal/reconcile/{plan_id}/discard")
async def mal_discard(plan_id: int, _: None = Depends(require_session)):
    mal_reconcile.discard(plan_id)
    return _redirect("/mal", "Preview discarded. Nothing was written.")
