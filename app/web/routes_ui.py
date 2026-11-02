"""Server-rendered UI: dashboard, activity log, system status, settings.

No build chain, no CDN: Jinja templates and one stylesheet, so the container
works on a LAN with no internet access.

The dashboard is for what needs you (setup, titles waiting for a rating) and
what just happened; the System page holds the machinery (AniList API health,
datasets, scheduled jobs, the webhook URL). The review queue (/review) is where
held and unmatched events are linked to AniList titles over episode ranges.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, config, db, repo, settings_store, tokens
from app.anilist.client import (
    AniListAuthError,
    AniListClient,
    AniListError,
    AniListUnavailable,
)
from app.anilist.client import health as anilist_health
from app.anilist.client import limiter as anilist_limiter
from app.datasets import refresh as dataset_refresh
from app.mapping import engine
from app.plex import client as plex_client
from app.plex.client import PlexClient, PlexUnavailable
from app.sync import completion, reconcile, retry, review
from app.web import timefmt

log = logging.getLogger(__name__)

router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
timefmt.register(templates.env)

# Every status an event can end in, in the order the activity filter shows them.
EVENT_STATUSES = [
    "written",
    "would_write",
    "awaiting_rating",
    "no_change",
    "deferred",
    "held",
    "unmatched",
    "skipped",
    "error",
    "duplicate",
]

# Background work started from a button. Held here so the tasks are not
# garbage-collected mid-run.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def anilist_status() -> dict[str, Any]:
    """The AniList health snapshot, plus the token and the deferred backlog."""
    limiter = anilist_limiter()
    snap = anilist_health().snapshot(limit=limiter.limit, remaining=limiter.remaining)
    token = tokens.get(tokens.ANILIST)
    snap["token_status"] = token.status
    snap["token_broken"] = token.status == tokens.STATUS_BROKEN
    snap["deferred"] = repo.deferred_count()
    snap["queued_ratings"] = completion.queued_count()
    snap["next_retry"] = repo.next_deferred_attempt()
    snap["retry_running"] = retry.is_running()
    snap["retry_blocked"] = retry.blocked_reason()
    return snap


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def require_session(request: Request) -> None:
    """Allow everything until a password is set, so first-run setup is possible."""
    if not auth.password_is_set():
        return
    if auth.session_is_valid(request.cookies.get(auth.COOKIE_NAME)):
        return
    raise HTTPException(status_code=303, headers={"Location": "/login"})


def _remember_base_url(request: Request) -> None:
    """Where the UI is reached, so a notification can link back to it."""
    base = str(request.base_url).rstrip("/")
    if base and settings_store.get("ui_base_url", "") != base:
        settings_store.set("ui_base_url", base)


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    _remember_base_url(request)
    level = request.query_params.get("level", "info")
    context = {
        "request": request,
        "dry_run": settings_store.is_dry_run(),
        "password_set": auth.password_is_set(),
        "pending_review": review.open_count(),
        "to_rate": completion.open_count(),
        "app_version": config.APP_VERSION,
        "anilist": anilist_status(),
        "notice_level": level if level in ("info", "ok", "error") else "info",
    }
    context.update(extra)
    return context


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    if not auth.password_is_set():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", _base_context(request, error=None)
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    stored = settings_store.get("admin_password_hash", "")
    if not stored or not auth.verify_password(password, stored):
        return templates.TemplateResponse(
            request,
            "login.html",
            _base_context(request, error="That password did not match."),
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _: None = Depends(require_session),
    notice: str | None = None,
) -> HTMLResponse:
    """What needs you, and what just happened. The machinery is on /system."""
    from app import setup as setup_state

    if setup_state.is_fresh_install():
        return RedirectResponse("/setup", status_code=303)

    server = plex_client.stored_server()
    account_row = db.query_one("SELECT id, name FROM plex_account LIMIT 1")
    datasets = dataset_refresh.status()
    last_webhook = _watermark("last_webhook")
    setup_steps = setup_state.steps()

    context = _base_context(
        request,
        plex_configured=server is not None,
        plex_account=dict(account_row) if account_row else None,
        last_webhook=last_webhook,
        datasets=datasets,
        datasets_ok=bool(datasets) and all(d.get("status") == "ok" for d in datasets),
        day_counts=repo.status_counts(since_hours=24),
        recent=repo.recent_events(limit=10),
        ratings=completion.list_open(),
        webhook_url=_webhook_url(request),
        setup_steps=setup_steps,
        setup_remaining=len(setup_state.remaining(setup_steps)),
        notice=notice,
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/system", response_class=HTMLResponse)
async def system_page(
    request: Request,
    _: None = Depends(require_session),
    notice: str | None = None,
) -> HTMLResponse:
    """AniList API health, connections, datasets, jobs and the webhook URL."""
    from app import scheduler

    server = plex_client.stored_server()
    account_row = db.query_one("SELECT id, name FROM plex_account LIMIT 1")
    enabled = db.query("SELECT section_id, title FROM library WHERE enabled = 1")

    context = _base_context(
        request,
        plex_configured=server is not None,
        plex_base_url=server[0] if server else None,
        plex_account=dict(account_row) if account_row else None,
        enabled_libraries=[dict(r) for r in enabled],
        token_status=tokens.all_statuses(),
        last_webhook=_watermark("last_webhook"),
        last_reconcile=reconcile.last_run_at(),
        last_reconcile_summary=_watermark("last_reconcile_summary"),
        last_retry=_watermark("last_anilist_retry"),
        last_retry_summary=_watermark("last_anilist_retry_summary"),
        datasets=dataset_refresh.status(),
        counters={
            "accepted": db.get_counter("webhook_accepted"),
            "dropped_other_accounts": db.get_counter("foreign_event_dropped"),
            "ignored": db.get_counter("webhook_ignored_event_type"),
        },
        event_total=repo.event_count(),
        webhook_url=_webhook_url(request),
        jobs=scheduler.job_states(),
        mal_blocked=_mal_blocked(),
        mal_last_poll=_watermark("mal_last_poll"),
        mal_last_summary=_watermark("mal_last_poll_summary"),
        notice=notice,
    )
    return templates.TemplateResponse(request, "system.html", context)


def _mal_blocked() -> str | None:
    from app.mal import sync as mal_sync

    return mal_sync.blocked_reason()


# --------------------------------------------------------------------------
# rating a finished series
# --------------------------------------------------------------------------
@router.post("/ratings/{pending_id}")
async def rate_series(
    pending_id: int,
    _: None = Depends(require_session),
    action: str = Form("rate"),
    score: str = Form(""),
):
    """Rate and complete, complete without a rating, or stop waiting."""
    if action == "dismiss":
        outcome = completion.dismiss(pending_id)
        return _redirect("/#rate", outcome.detail, "ok" if outcome.ok else "error")

    value: float | None = None
    if action != "no_rating":
        try:
            value = completion.parse_score(score)
        except ValueError as exc:
            return _redirect("/#rate", f"Not saved: {exc}.", "error")

    outcome = await completion.submit(pending_id, value)
    if not outcome.ok:
        level = "error"
    elif outcome.status == completion.QUEUED:
        level = "info"
    else:
        level = "ok"
    return _redirect("/#rate", outcome.detail, level)


def _watermark(name: str) -> str | None:
    row = db.query_one("SELECT value FROM watermark WHERE name = ?", (name,))
    return row["value"] if row else None


def _webhook_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/webhook/plex/{auth.webhook_path_token()}"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, _: None = Depends(require_session), notice: str | None = None
) -> HTMLResponse:
    server = plex_client.stored_server()
    accounts: list[dict[str, Any]] = []
    libraries: list[dict[str, Any]] = []
    plex_error: str | None = None

    if server:
        try:
            client = PlexClient(*server)
            accounts = [
                {"id": a.id, "name": a.name, "thumb": a.thumb}
                for a in await plex_client.run(client.accounts_sync)
            ]
            libraries = [
                {"section_id": lib.section_id, "title": lib.title, "kind": lib.kind}
                for lib in await plex_client.run(client.libraries_sync)
            ]
        except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
            plex_error = str(exc)

    enabled = plex_client.enabled_section_ids()
    account_row = db.query_one("SELECT id FROM plex_account LIMIT 1")

    context = _base_context(
        request,
        settings=settings_store.all_settings(),
        plex_base_url=server[0] if server else "",
        plex_token_set=bool(server),
        plex_error=plex_error,
        accounts=accounts,
        libraries=libraries,
        enabled_sections=enabled,
        chosen_account=int(account_row["id"]) if account_row else None,
        token_status=tokens.all_statuses(),
        notifications=_recent_notifications(),
        notice=notice,
    )
    return templates.TemplateResponse(request, "settings.html", context)


def _recent_notifications() -> list[dict[str, Any]]:
    from app import notify

    return notify.recent()


@router.post("/settings/plex")
async def save_plex(
    request: Request,
    _: None = Depends(require_session),
    base_url: str = Form(...),
    token: str = Form(""),
):
    existing = plex_client.stored_server()
    effective_token = token.strip() or (existing[1] if existing else "")
    if not effective_token:
        return _redirect("/settings#plex", "A Plex token is required", "error")

    client = PlexClient(base_url.strip(), effective_token)
    try:
        info = await plex_client.run(client.check_sync)
    except PlexUnavailable as exc:
        return _redirect("/settings#plex", str(exc), "error")

    plex_client.save_server(
        base_url.strip(),
        effective_token,
        name=info.get("name"),
        machine_id=info.get("machine_id"),
    )
    return _redirect("/settings#account", f"Connected to {info.get('name')}", "ok")


@router.post("/settings/account")
async def save_account(
    _: None = Depends(require_session),
    account_id: int = Form(...),
    account_name: str = Form(""),
):
    plex_client.save_account(
        plex_client.PlexAccountInfo(int(account_id), account_name or str(account_id), None)
    )
    return _redirect(
        "/settings#libraries",
        f"Only events from {account_name or account_id} will be processed",
        "ok",
    )


@router.post("/settings/libraries")
async def save_libraries(request: Request, _: None = Depends(require_session)):
    form = await request.form()
    chosen = {int(v) for v in form.getlist("section_id")}
    await plex_client.save_libraries_from_server(chosen)
    return _redirect("/settings#anilist", f"{len(chosen)} library(ies) enabled", "ok")


@router.post("/settings/anilist-token")
async def save_anilist_token(
    _: None = Depends(require_session), access_token: str = Form(...)
):
    """Paste a token: the fallback for the OAuth sign-in in the setup wizard."""
    message, level = await connect_anilist(access_token)
    return _redirect("/settings#anilist", message, level)


async def connect_anilist(access_token: str, *, expires_at=None) -> tuple[str, str]:
    """Check a token against AniList and store it if it works. Returns (message, level).

    Shared by the pasted-token form and the OAuth callback, so a token is
    checked the same way however it arrived.
    """
    stripped = (access_token or "").strip()
    if not stripped:
        return "No token given", "error"

    # A token being *tried* must not mark the stored one broken if it is wrong,
    # must be allowed through an open circuit, and must not hang the page
    # behind a long rate-limit wait.
    client = AniListClient(
        stripped,
        max_wait=15.0,
        max_attempts=2,
        respect_circuit=False,
        record_auth_failures=False,
    )
    try:
        viewer = await client.viewer()
    except AniListUnavailable as exc:
        return f"AniList could not be reached, so the token was not checked or saved: {exc}", "error"
    except (AniListAuthError, AniListError) as exc:
        return f"Token not saved: {exc}", "error"
    finally:
        await client.__aexit__(None, None, None)

    tokens.save(
        tokens.ANILIST,
        access_token=stripped,
        expires_at=expires_at,
        account_name=viewer.get("name"),
        account_id=viewer.get("id"),
        status=tokens.STATUS_OK,
        last_error=None,
    )
    message = f"Connected to AniList as {viewer.get('name')}."
    waiting = repo.deferred_count() + completion.queued_count()
    if waiting:
        # Work deferred while the old token was broken can go now.
        _spawn(retry.retry_deferred(force=True))
        message += f" Retrying {waiting} waiting item(s) in the background."
    return message, "ok"


@router.post("/settings/behaviour")
async def save_behaviour(request: Request, _: None = Depends(require_session)):
    from app import scheduler

    form = await request.form()

    def flag(name: str) -> bool:
        return form.get(name) is not None

    def number(name: str, default: int, *, low: int, high: int) -> int:
        try:
            value = int(str(form.get(name, default)).strip())
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    settings_store.set_many(
        {
            "dry_run": flag("dry_run"),
            "sync_enabled": flag("sync_enabled"),
            "skip_specials": flag("skip_specials"),
            "add_untracked": flag("add_untracked"),
            "set_dates": flag("set_dates"),
            "rate_before_complete": flag("rate_before_complete"),
            "mal_sync_enabled": flag("mal_sync_enabled"),
            "mal_sync_score": flag("mal_sync_score"),
            "mal_sync_dates": flag("mal_sync_dates"),
            "mal_poll_interval_minutes": number("mal_poll_interval_minutes", 15, low=5, high=1440),
            "uncertain_action": (
                "apply_and_flag"
                if form.get("uncertain_action") == "apply_and_flag"
                else "hold"
            ),
            "rewatch_action": (
                "repeating" if form.get("rewatch_action") == "repeating" else "ignore"
            ),
            "reconcile_interval_hours": number(
                "reconcile_interval_hours", 6, low=1, high=168
            ),
            "reconcile_lookback_hours": number(
                "reconcile_lookback_hours", 72, low=1, high=720
            ),
            "dataset_refresh_hours": number("dataset_refresh_hours", 24, low=1, high=168),
            "anilist_retry_interval_minutes": number(
                "anilist_retry_interval_minutes", 5, low=1, high=240
            ),
            "anilist_retry_give_up_days": number(
                "anilist_retry_give_up_days", 7, low=1, high=60
            ),
            "anilist_max_wait_seconds": number(
                "anilist_max_wait_seconds", 120, low=5, high=600
            ),
            "notify_webhook_url": str(form.get("notify_webhook_url", "")).strip(),
            "notify_service": (
                str(form.get("notify_service"))
                if form.get("notify_service") in ("auto", "discord", "ntfy", "json")
                else "auto"
            ),
            "notify_on_review": flag("notify_on_review"),
            "notify_on_rating": flag("notify_on_rating"),
            "notify_on_token_error": flag("notify_on_token_error"),
        }
    )
    scheduler.apply_settings()
    return _redirect("/settings#behaviour", "Settings saved", "ok")


@router.post("/settings/notify-test")
async def notify_test(request: Request, _: None = Depends(require_session)):
    """Send a test to the URL in the form, saved or not. Saving is separate."""
    from app import notify

    form = await request.form()
    ok, message = await notify.send_test(
        url=str(form.get("notify_webhook_url", "") or "").strip() or None,
        service_name=str(form.get("notify_service", "") or "") or None,
    )
    return _redirect("/settings#notifications", message, "ok" if ok else "error")


@router.post("/settings/password")
async def save_password(_: None = Depends(require_session), password: str = Form(...)):
    if len(password) < 8:
        return _redirect("/settings#password", "Use at least 8 characters", "error")
    auth.set_password(password)
    response = _redirect("/settings#password", "Password set", "ok")
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
@router.post("/actions/refresh-datasets")
async def action_refresh_datasets(_: None = Depends(require_session)):
    results = await dataset_refresh.refresh_all(force=True)
    detail = "; ".join(f"{r.name}: {r.status} ({r.detail})" for r in results)
    level = "error" if any(r.status == "error" for r in results) else "ok"
    return _redirect("/system#datasets", detail, level)


@router.post("/actions/reconcile")
async def action_reconcile(_: None = Depends(require_session)):
    """Kick the reconcile job rather than run it inside the request.

    A sweep paces itself under AniList's rate limit, so it can take minutes;
    the page should not hang for that long.
    """
    from app import scheduler

    if scheduler.run_now(scheduler.JOB_RECONCILE):
        return _redirect(
            "/system#jobs",
            "Reconcile started in the background. Results appear in Activity as "
            "they are processed, and the summary on this page when it finishes.",
            "ok",
        )
    report = await reconcile.run_reconcile()
    return _redirect("/system#jobs", report.detail or report.summary())


@router.post("/actions/check-anilist")
async def action_check_anilist(_: None = Depends(require_session)):
    """Make one request to AniList now, through an open circuit if need be."""
    try:
        client = AniListClient.from_db(max_wait=15.0, max_attempts=1, respect_circuit=False)
    except AniListAuthError as exc:
        return _redirect("/system#anilist", f"AniList check: {exc}", "error")
    try:
        viewer = await client.viewer()
    except AniListUnavailable as exc:
        return _redirect("/system#anilist", f"AniList is still unavailable: {exc}", "error")
    except (AniListAuthError, AniListError) as exc:
        return _redirect("/system#anilist", f"AniList check failed: {exc}", "error")
    finally:
        await client.__aexit__(None, None, None)

    waiting = repo.deferred_count() + completion.queued_count()
    message = f"AniList is responding (signed in as {viewer.get('name')})."
    if waiting:
        _spawn(retry.retry_deferred(force=True))
        message += f" Retrying {waiting} waiting item(s) in the background."
    return _redirect("/system#anilist", message, "ok")


@router.post("/actions/retry-deferred")
async def action_retry_deferred(
    _: None = Depends(require_session), back: str = Form("/")
):
    back = back if back.startswith("/") and not back.startswith("//") else "/"
    if retry.is_running():
        return _redirect(back, "A retry run is already in progress.")
    reason = retry.blocked_reason()
    if reason:
        return _redirect(back, f"Not retrying yet: {reason}.", "error")
    count = repo.deferred_count() + completion.queued_count()
    if not count:
        return _redirect(back, "There are no deferred events to retry.")
    _spawn(retry.retry_deferred(force=True))
    return _redirect(
        back,
        f"Retrying up to {min(count, retry.BATCH_SIZE)} of {count} waiting item(s) "
        "in the background. Refresh to see progress.",
        "ok",
    )


# --------------------------------------------------------------------------
# activity log
# --------------------------------------------------------------------------
@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    _: None = Depends(require_session),
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    page = max(1, page)
    per_page = 50
    events = repo.recent_events(
        limit=per_page, offset=(page - 1) * per_page, status=status, search=q
    )
    context = _base_context(
        request,
        events=events,
        status=status,
        q=q or "",
        page=page,
        per_page=per_page,
        total=repo.event_count(status=status),
        statuses=EVENT_STATUSES,
        status_counts=repo.status_counts(),
        all_total=repo.event_count(),
    )
    return templates.TemplateResponse(request, "activity.html", context)


@router.post("/activity/{event_id}/retry")
async def activity_retry(event_id: int, _: None = Depends(require_session)):
    status, detail = await retry.retry_event(event_id)
    if status is None:
        return _redirect(f"/activity/{event_id}", f"Not retried: {detail}", "error")
    level = "error" if status in ("deferred", "error") else "ok"
    return _redirect(f"/activity/{event_id}", f"Retried: {status}. {detail}", level)


@router.get("/activity/{event_id}", response_class=HTMLResponse)
async def activity_detail(
    request: Request,
    event_id: int,
    _: None = Depends(require_session),
    notice: str | None = None,
) -> HTMLResponse:
    import json

    row = db.query_one("SELECT * FROM event WHERE id = ?", (event_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="no such event")

    event = dict(row)
    trace = None
    if event.get("trace_json"):
        try:
            trace = json.loads(event["trace_json"])
        except ValueError:
            trace = None

    writes = []
    for write in repo.writes_for_event(event_id):
        for key in ("request_json", "before_json", "after_json"):
            if write.get(key):
                try:
                    write[key] = json.dumps(json.loads(write[key]), indent=2)
                except ValueError:
                    pass
        writes.append(write)

    return templates.TemplateResponse(
        request,
        "event.html",
        _base_context(
            request,
            event=event,
            trace=trace,
            writes=writes,
            retryable=event.get("status") in retry.RETRYABLE,
            reviewable=event.get("status") in review.REVIEW_STATUSES,
            pending_rating=(
                completion.open_for(int(event["anilist_id"]))
                if event.get("status") == "awaiting_rating" and event.get("anilist_id")
                else None
            ),
            notice=notice,
        ),
    )


def _q(text: str) -> str:
    return quote_plus(text[:400])


def _redirect(path: str, notice: str, level: str = "info") -> RedirectResponse:
    """Redirect with a notice. Any #fragment is kept after the query string."""
    base, _, fragment = path.partition("#")
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}notice={_q(notice)}&level={level}"
    if fragment:
        url += f"#{fragment}"
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------
# review queue
# --------------------------------------------------------------------------
@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request, _: None = Depends(require_session), notice: str | None = None
) -> HTMLResponse:
    groups = review.open_groups()
    for group in groups:
        group.covered_ids = _now_overridden(group.event_ids)
    context = _base_context(
        request,
        groups=groups,
        overrides=review.list_overrides(),
        notice=notice,
    )
    return templates.TemplateResponse(request, "review.html", context)


def _now_overridden(event_ids: list[int]) -> list[int]:
    """The events a manual correction would match if they were re-run now."""
    covered = []
    for event_id in event_ids:
        event = repo.event_input(event_id)
        if event is not None and engine.resolve(event).confidence == "override":
            covered.append(event_id)
    return covered


@router.get("/review/{event_id}", response_class=HTMLResponse)
async def review_link_page(
    request: Request,
    event_id: int,
    _: None = Depends(require_session),
    q: str | None = None,
    anilist: str | None = None,
    ep_from: str | None = None,
    ep_to: str | None = None,
    start: str | None = None,
    scope: str = review.SCOPE_SHOW,
    notice: str | None = None,
) -> HTMLResponse:
    group = review.group_for_event(event_id)
    if group is None:
        return _redirect("/review", "That item is no longer waiting for review.")

    client = _anilist_for_ui()
    results: list[dict[str, Any]] = []
    search_error: str | None = None
    query = (q or "").strip() or (group.title if anilist is None and not group.candidates else "")
    if query:
        if client is None:
            search_error = "Connect AniList to search. You can still paste an AniList id or URL."
        else:
            try:
                results = await client.search(query, per_page=10)
                for media in results:
                    repo.cache_media(media)
            except AniListError as exc:
                search_error = f"AniList search failed: {exc}. You can still paste an AniList id or URL."

    chosen: dict[str, Any] | None = None
    spec = None
    rows: list[dict[str, Any]] = []
    form_error: str | None = None
    chosen_id = review.parse_anilist_ref(anilist)
    if anilist and chosen_id is None:
        form_error = "That is not an AniList id or anilist.co link."
    if chosen_id is not None:
        chosen = await _media_summary(chosen_id, client)
        candidate = next((c for c in group.candidates if c["anilist_id"] == chosen_id), None)
        if ep_from is None and start is None:
            spec = review.suggest_range(group, candidate)
        else:
            try:
                spec = review.parse_range(ep_from, ep_to, start)
            except ValueError as exc:
                form_error = str(exc)
                spec = review.suggest_range(group, candidate)
        rows = review.preview(
            spec,
            season_end=review.season_size(group.plex_guid, group.season),
            total=chosen.get("episodes"),
            watched=group.episodes,
        )

    if client is not None:
        await client.__aexit__(None, None, None)

    context = _base_context(
        request,
        group=group,
        query=q or query,
        results=results,
        search_error=search_error,
        chosen=chosen,
        spec=spec,
        rows=rows,
        scope=scope if scope in (review.SCOPE_SHOW, review.SCOPE_TVDB) else review.SCOPE_SHOW,
        season_end=review.season_size(group.plex_guid, group.season),
        form_error=form_error,
        notice=notice,
    )
    return templates.TemplateResponse(request, "review_link.html", context)


@router.post("/review/{event_id}/link")
async def review_link_save(
    event_id: int,
    _: None = Depends(require_session),
    anilist: str = Form(...),
    ep_from: str = Form("1"),
    ep_to: str = Form(""),
    start: str = Form("1"),
    scope: str = Form(review.SCOPE_SHOW),
):
    group = review.group_for_event(event_id)
    if group is None:
        return _redirect("/review", "That item is no longer waiting for review.")
    back = f"/review/{event_id}?anilist={_q(anilist)}&ep_from={_q(ep_from)}&ep_to={_q(ep_to)}&start={_q(start)}"

    anilist_id = review.parse_anilist_ref(anilist)
    if anilist_id is None:
        return _redirect(back, "Not saved: that is not an AniList id or link.", "error")
    try:
        spec = review.parse_range(ep_from, ep_to, start)
        review.save_override(group, anilist_id=anilist_id, spec=spec, scope=scope)
    except ValueError as exc:
        return _redirect(back, f"Not saved: {exc}.", "error")

    message = f"Correction saved for {group.label}."
    covered = review.covered_events(group, spec)
    if not covered:
        message += " None of the waiting episodes fall in that range, so they stay in the queue."
        return _redirect("/review", message, "info")
    outcome, ran = await _rerun(covered)
    return _redirect("/review", f"{message} {outcome}", "ok" if ran else "info")


@router.post("/review/{event_id}/rerun")
async def review_rerun(event_id: int, _: None = Depends(require_session)):
    group = review.group_for_event(event_id)
    if group is None:
        return _redirect("/review", "That item is no longer waiting for review.")
    covered = _now_overridden(group.event_ids)
    if not covered:
        return _redirect("/review", "No correction covers these episodes yet.", "error")
    outcome, ran = await _rerun(covered)
    return _redirect("/review", outcome, "ok" if ran else "info")


@router.post("/review/{event_id}/dismiss")
async def review_dismiss(event_id: int, _: None = Depends(require_session)):
    group = review.group_for_event(event_id)
    if group is None:
        return _redirect("/review", "That item is no longer waiting for review.")
    count = review.dismiss(group)
    return _redirect("/review", f"Dismissed {group.label} ({count} event(s)); nothing was written.", "ok")


@router.post("/review/corrections/{override_id}/delete")
async def review_delete_override(override_id: int, _: None = Depends(require_session)):
    if review.delete_override(override_id):
        return _redirect(
            "/review#corrections",
            "Correction deleted. Episodes watched from now on are matched by the datasets again; "
            "nothing already written is changed.",
            "ok",
        )
    return _redirect("/review#corrections", "That correction no longer exists.", "error")


# Above this many events a re-run goes to the background rather than holding the page.
_RERUN_INLINE = 10


async def _rerun(event_ids: list[int]) -> tuple[str, bool]:
    """Re-run events through the pipeline. Returns (message, whether it ran)."""
    client = _anilist_for_ui(max_wait=30.0)
    if client is None:
        message = (
            "AniList is not connected, so the episodes were not re-run yet; "
            "use Re-run once it is."
        )
        return message, False
    plex = None
    try:
        plex = plex_client.PlexClient.from_db()
    except PlexUnavailable:
        plex = None

    async def run() -> str:
        try:
            report = await review.reprocess(event_ids, anilist=client, plex=plex)
        finally:
            await client.__aexit__(None, None, None)
        return report.summary().capitalize() + "."

    if len(event_ids) > _RERUN_INLINE:
        _spawn(run())
        return f"Re-running {len(event_ids)} event(s) in the background; see Activity.", True
    return await run(), True


def _anilist_for_ui(**kwargs) -> AniListClient | None:
    kwargs.setdefault("max_wait", 15.0)
    kwargs.setdefault("max_attempts", 2)
    try:
        return AniListClient.from_db(**kwargs)
    except AniListAuthError:
        return None


async def _media_summary(anilist_id: int, client: AniListClient | None) -> dict[str, Any]:
    """Title and episode count for the editor: the cache, else one AniList read."""
    info = engine.media_info(anilist_id)
    if (not info or not info.get("episodes")) and client is not None:
        try:
            _state, media = await client.media_with_entry(anilist_id)
            repo.cache_media(media)
            info = engine.media_info(anilist_id)
        except AniListError:
            pass
    info = info or {}
    return {
        "anilist_id": anilist_id,
        "title": info.get("title_english") or info.get("title_romaji") or f"AniList {anilist_id}",
        "episodes": info.get("episodes"),
        "format": info.get("format"),
        "known": bool(info),
    }
