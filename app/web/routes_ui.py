"""Server-rendered UI: dashboard, settings, activity log.

No build chain, no CDN: Jinja templates and one stylesheet, so the container
works on a LAN with no internet access. The review queue, search-and-link and
the visual range editor come next; everything here is what is needed to
configure the bridge and watch it work in dry-run.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, config, db, repo, settings_store, tokens
from app.anilist.client import AniListAuthError, AniListClient, AniListError
from app.anilist.client import limiter as anilist_limiter
from app.datasets import refresh as dataset_refresh
from app.plex import client as plex_client
from app.plex.client import PlexClient, PlexUnavailable
from app.sync import reconcile

log = logging.getLogger(__name__)

router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


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
    context = {
        "request": request,
        "dry_run": settings_store.is_dry_run(),
        "password_set": auth.password_is_set(),
        "pending_review": repo.pending_review_count(),
        "app_version": config.APP_VERSION,
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
    from app import scheduler

    server = plex_client.stored_server()
    account_row = db.query_one("SELECT id, name FROM plex_account LIMIT 1")
    enabled = db.query("SELECT section_id, title FROM library WHERE enabled = 1")

    limiter = anilist_limiter()
    context = _base_context(
        request,
        plex_configured=server is not None,
        plex_base_url=server[0] if server else None,
        plex_account=dict(account_row) if account_row else None,
        enabled_libraries=[dict(r) for r in enabled],
        token_status=tokens.all_statuses(),
        last_webhook=_watermark("last_webhook"),
        last_reconcile=reconcile.last_run_at(),
        datasets=dataset_refresh.status(),
        counters={
            "accepted": db.get_counter("webhook_accepted"),
            "dropped_other_accounts": db.get_counter("foreign_event_dropped"),
            "ignored": db.get_counter("webhook_ignored_event_type"),
        },
        event_total=repo.event_count(),
        webhook_url=_webhook_url(request),
        jobs=scheduler.job_states(),
        anilist_limit=limiter.limit,
        anilist_remaining=limiter.remaining,
        notice=notice,
    )
    return templates.TemplateResponse(request, "dashboard.html", context)


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
        return RedirectResponse(
            "/settings?notice=A+Plex+token+is+required", status_code=303
        )

    client = PlexClient(base_url.strip(), effective_token)
    try:
        info = await plex_client.run(client.check_sync)
    except PlexUnavailable as exc:
        return RedirectResponse(f"/settings?notice={_q(str(exc))}", status_code=303)

    plex_client.save_server(
        base_url.strip(),
        effective_token,
        name=info.get("name"),
        machine_id=info.get("machine_id"),
    )
    return RedirectResponse(
        f"/settings?notice={_q('Connected to ' + str(info.get('name')))}", status_code=303
    )


@router.post("/settings/account")
async def save_account(
    _: None = Depends(require_session),
    account_id: int = Form(...),
    account_name: str = Form(""),
):
    plex_client.save_account(
        plex_client.PlexAccountInfo(int(account_id), account_name or str(account_id), None)
    )
    return RedirectResponse(
        f"/settings?notice={_q('Only events from ' + (account_name or str(account_id)) + ' will be processed')}",
        status_code=303,
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
    return RedirectResponse(
        f"/settings?notice={_q(f'{len(chosen)} library(ies) enabled')}", status_code=303
    )


@router.post("/settings/anilist-token")
async def save_anilist_token(
    _: None = Depends(require_session), access_token: str = Form(...)
):
    """Interim: paste a token. The OAuth browser flow replaces this in step 5."""
    stripped = access_token.strip()
    if not stripped:
        return RedirectResponse("/settings?notice=No+token+given", status_code=303)

    client = AniListClient(stripped)
    try:
        viewer = await client.viewer()
    except (AniListAuthError, AniListError) as exc:
        return RedirectResponse(f"/settings?notice={_q(str(exc))}", status_code=303)
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
    return RedirectResponse(
        f"/settings?notice={_q('Connected to AniList as ' + str(viewer.get('name')))}",
        status_code=303,
    )


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
            "notify_webhook_url": str(form.get("notify_webhook_url", "")).strip(),
        }
    )
    scheduler.apply_settings()
    return RedirectResponse("/settings?notice=Settings+saved", status_code=303)


@router.post("/settings/password")
async def save_password(_: None = Depends(require_session), password: str = Form(...)):
    if len(password) < 8:
        return RedirectResponse(
            "/settings?notice=Use+at+least+8+characters", status_code=303
        )
    auth.set_password(password)
    response = RedirectResponse("/settings?notice=Password+set", status_code=303)
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
    return RedirectResponse(f"/?notice={_q(detail)}", status_code=303)


@router.post("/actions/reconcile")
async def action_reconcile(_: None = Depends(require_session)):
    report = await reconcile.run_reconcile()
    return RedirectResponse(f"/?notice={_q(report.detail or report.summary())}", status_code=303)


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
        statuses=[
            "written",
            "would_write",
            "no_change",
            "held",
            "unmatched",
            "skipped",
            "error",
            "duplicate",
        ],
    )
    return templates.TemplateResponse(request, "activity.html", context)


@router.get("/activity/{event_id}", response_class=HTMLResponse)
async def activity_detail(
    request: Request, event_id: int, _: None = Depends(require_session)
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
        _base_context(request, event=event, trace=trace, writes=writes),
    )


def _q(text: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(text[:400])
