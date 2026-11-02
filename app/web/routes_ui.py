"""Server-rendered UI: dashboard, activity log, system status, settings.

No build chain, no CDN: Jinja templates and one stylesheet, so the container
works on a LAN with no internet access.

The dashboard is for what needs you (setup, titles waiting for a rating) and
what just happened; the System page holds the machinery (AniList API health,
datasets, scheduled jobs, the webhook URL). The review queue, search-and-link
and the visual range editor come next.
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
from app.plex import client as plex_client
from app.plex.client import PlexClient, PlexUnavailable
from app.sync import completion, reconcile, retry
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


def _base_context(request: Request, **extra: Any) -> dict[str, Any]:
    level = request.query_params.get("level", "info")
    context = {
        "request": request,
        "dry_run": settings_store.is_dry_run(),
        "password_set": auth.password_is_set(),
        "pending_review": repo.pending_review_count(),
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
    server = plex_client.stored_server()
    account_row = db.query_one("SELECT id, name FROM plex_account LIMIT 1")
    enabled = db.query("SELECT section_id FROM library WHERE enabled = 1 LIMIT 1")
    token_status = tokens.all_statuses()
    datasets = dataset_refresh.status()
    last_webhook = _watermark("last_webhook")

    setup_steps = [
        {"label": "Connect your Plex server", "done": server is not None,
         "href": "/settings#plex"},
        {"label": "Choose which Plex account counts", "done": account_row is not None,
         "href": "/settings#account"},
        {"label": "Tick your anime libraries", "done": bool(enabled),
         "href": "/settings#libraries"},
        {"label": "Connect AniList", "done": token_status["anilist"].present,
         "href": "/settings#anilist"},
        {"label": "Set an admin password", "done": auth.password_is_set(),
         "href": "/settings#password"},
        {"label": "Add the webhook in Plex", "done": last_webhook is not None,
         "href": "#setup-webhook"},
    ]

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
        setup_remaining=sum(1 for step in setup_steps if not step["done"]),
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
        notice=notice,
    )
    return templates.TemplateResponse(request, "system.html", context)


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
        notice=notice,
    )
    return templates.TemplateResponse(request, "settings.html", context)


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
    server = plex_client.stored_server()
    if server:
        client = PlexClient(*server)
        try:
            libraries = await plex_client.run(client.libraries_sync)
        except Exception:  # noqa: BLE001
            libraries = []
        with db.session() as conn:
            for lib in libraries:
                conn.execute(
                    "INSERT INTO library (section_id, title, kind, enabled)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (section_id) DO UPDATE SET title = excluded.title,"
                    "   kind = excluded.kind, enabled = excluded.enabled",
                    (lib.section_id, lib.title, lib.kind, 1 if lib.section_id in chosen else 0),
                )
    return _redirect("/settings#anilist", f"{len(chosen)} library(ies) enabled", "ok")


@router.post("/settings/anilist-token")
async def save_anilist_token(
    _: None = Depends(require_session), access_token: str = Form(...)
):
    """Interim: paste a token. The OAuth browser flow replaces this in step 5."""
    stripped = access_token.strip()
    if not stripped:
        return _redirect("/settings#anilist", "No token given", "error")

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
        return _redirect(
            "/settings#anilist",
            f"AniList could not be reached, so the token was not checked or saved: {exc}",
            "error",
        )
    except (AniListAuthError, AniListError) as exc:
        return _redirect("/settings#anilist", f"Token not saved: {exc}", "error")
    finally:
        await client.__aexit__(None, None, None)

    tokens.save(
        tokens.ANILIST,
        access_token=stripped,
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
    return _redirect("/settings#anilist", message, "ok")


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
        }
    )
    scheduler.apply_settings()
    return _redirect("/settings#behaviour", "Settings saved", "ok")


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
