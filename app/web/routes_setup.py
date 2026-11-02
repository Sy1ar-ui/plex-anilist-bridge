"""The setup wizard: one page per step, each skippable, all state in the database.

The order is deliberate. The admin password comes first because every later
step stores a token. Plex sign-in comes before the account and library steps
because those are read from the chosen server.

Every network call here (plex.tv, the user's Plex server, AniList, MAL) goes
through a module that accepts an injected HTTP client, so the wizard is tested
without a network.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import auth, db, oauth, settings_store, tokens
from app import setup as setup_state
from app.plex import account as plex_account
from app.plex import client as plex_client
from app.plex.client import PlexClient, PlexUnavailable
from app.web.routes_ui import (
    _base_context,
    _redirect,
    _watermark,
    _webhook_url,
    connect_anilist,
    require_session,
    templates,
)

log = logging.getLogger(__name__)

router = APIRouter()

_SIGNED_IN_AS = "plex_signed_in_as"
_ANIME_NAME = re.compile(r"anime|アニメ|animation", re.IGNORECASE)


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _page(request: Request, step: str, **extra: Any) -> HTMLResponse:
    steps = setup_state.steps()
    context = _base_context(
        request,
        step=step,
        steps=steps,
        current=next((s for s in steps if s.key == step), None),
        next_key=setup_state.after(step) if step in setup_state.ORDER else None,
        remaining=setup_state.remaining(steps),
        notice=request.query_params.get("notice"),
        **extra,
    )
    return templates.TemplateResponse(request, "setup.html", context)


@router.get("/setup")
async def setup_start(_: None = Depends(require_session)):
    todo = setup_state.next_step()
    return RedirectResponse(todo.href if todo else "/setup/done", status_code=303)


@router.post("/setup/finish")
async def setup_finish(_: None = Depends(require_session)):
    """Finished, or "I'll do the rest later": either way, stop sending people here."""
    setup_state.mark_complete()
    return _redirect("/", "Setup saved. Dry run is still on: watch Activity for a while before turning it off.", "ok")


# --------------------------------------------------------------------------
# 1. admin password
# --------------------------------------------------------------------------
@router.get("/setup/password", response_class=HTMLResponse)
async def step_password(request: Request, _: None = Depends(require_session)):
    return _page(request, "password")


@router.post("/setup/password")
async def step_password_save(_: None = Depends(require_session), password: str = Form(...),
                             confirm: str = Form("")):
    if len(password) < 8:
        return _redirect("/setup/password", "Use at least 8 characters.", "error")
    if confirm != password:
        return _redirect("/setup/password", "The two passwords did not match.", "error")
    auth.set_password(password)
    response = _redirect("/setup/plex", "Password set.", "ok")
    response.set_cookie(auth.COOKIE_NAME, auth.issue_session(), max_age=auth.SESSION_MAX_AGE,
                        httponly=True, samesite="lax")
    return response


# --------------------------------------------------------------------------
# 2. Plex: sign in, then pick a server and an address that works from here
# --------------------------------------------------------------------------
@router.get("/setup/plex", response_class=HTMLResponse)
async def step_plex(request: Request, _: None = Depends(require_session)):
    pending = plex_account.pending()
    servers: list[plex_account.Server] = []
    error: str | None = None
    signed_in = pending.get("user") if pending.get("token") else None
    if pending.get("token"):
        try:
            servers = await plex_account.servers(pending["token"])
            for server in servers:
                await plex_account.probe(server)
        except plex_account.PlexAuthError as exc:
            error = str(exc)
    stored = plex_client.stored_server()
    server_row = db.query_one("SELECT name, base_url FROM plex_server WHERE id = 1")
    return _page(
        request,
        "plex",
        signed_in=signed_in,
        waiting_for_pin=bool(pending.get("pin_id")) and not pending.get("token"),
        servers=servers,
        plex_error=error,
        current_server=dict(server_row) if stored and server_row else None,
    )


@router.post("/setup/plex/start")
async def step_plex_start(request: Request, _: None = Depends(require_session)):
    try:
        pin = await plex_account.create_pin()
    except plex_account.PlexAuthError as exc:
        return _redirect("/setup/plex", f"Could not start the Plex sign-in: {exc}.", "error")
    nonce = plex_account.remember_pin(pin)
    forward = f"{_base(request)}/setup/plex/return?n={nonce}"
    return RedirectResponse(plex_account.auth_url(pin, forward), status_code=303)


@router.get("/setup/plex/return")
async def step_plex_return(_: None = Depends(require_session), n: str = ""):
    """Where plex.tv sends the browser back. Also used by "I've signed in"."""
    pending = plex_account.pending()
    if not pending.get("pin_id") or (n and n != pending.get("nonce")):
        return _redirect("/setup/plex", "That Plex sign-in is not the one started here; start again.", "error")
    try:
        token = await plex_account.check_pin(int(pending["pin_id"]))
        if token is None:
            return _redirect(
                "/setup/plex",
                "Plex has not confirmed the sign-in yet. Finish it in the Plex tab, then check again.",
            )
        who = await plex_account.user(token)
    except plex_account.PlexAuthError as exc:
        plex_account.forget()
        return _redirect("/setup/plex", f"Plex sign-in failed: {exc}.", "error")
    plex_account.remember_token(token, who)
    return _redirect("/setup/plex", f"Signed in to Plex as {who.title or who.username}. Now pick your server.", "ok")


@router.post("/setup/plex/choose")
async def step_plex_choose(_: None = Depends(require_session), server_id: str = Form(...),
                           uri: str = Form(...)):
    pending = plex_account.pending()
    if not pending.get("token"):
        return _redirect("/setup/plex", "Sign in to Plex first.", "error")
    try:
        servers = await plex_account.servers(pending["token"])
    except plex_account.PlexAuthError as exc:
        return _redirect("/setup/plex", str(exc), "error")
    server = next((s for s in servers if s.client_id == server_id), None)
    # The address must be one plex.tv listed for this server; never one from the form alone.
    if server is None or uri.rstrip("/") not in {c.uri for c in server.connections}:
        return _redirect("/setup/plex", "That server or address is not on your Plex account.", "error")

    client = PlexClient(uri, server.access_token)
    try:
        info = await plex_client.run(client.check_sync)
    except PlexUnavailable as exc:
        return _redirect("/setup/plex", f"{server.name} did not answer at {uri}: {exc}", "error")

    plex_client.save_server(uri, server.access_token, name=info.get("name"), machine_id=info.get("machine_id"))
    who = pending.get("user") or {}
    settings_store.set(_SIGNED_IN_AS, {"username": who.get("username"), "title": who.get("title"),
                                       "owned": server.owned})
    plex_account.forget()   # the account token is not kept; the server token is
    return _redirect("/setup/account", f"Connected to {info.get('name') or server.name}.", "ok")


@router.post("/setup/plex/cancel")
async def step_plex_cancel(_: None = Depends(require_session)):
    plex_account.forget()
    return _redirect("/setup/plex", "Plex sign-in cancelled.")


# --------------------------------------------------------------------------
# 3. account, 4. libraries
# --------------------------------------------------------------------------
@router.get("/setup/account", response_class=HTMLResponse)
async def step_account(request: Request, _: None = Depends(require_session)):
    accounts, error = [], None
    server = plex_client.stored_server()
    if server:
        try:
            accounts = await plex_client.run(PlexClient(*server).accounts_sync)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = str(exc)
    chosen = plex_client.configured_account_id()
    return _page(request, "account", accounts=accounts, plex_error=error,
                 chosen_account=chosen if chosen is not None else _guess_account(accounts),
                 has_server=server is not None)


def _guess_account(accounts: list[plex_client.PlexAccountInfo]) -> int | None:
    """Who signed in: the server owner is account 1; anyone else, by name."""
    hint = settings_store.get(_SIGNED_IN_AS, None) or {}
    if not isinstance(hint, dict) or not accounts:
        return None
    if hint.get("owned") and any(a.id == 1 for a in accounts):
        return 1
    names = {str(hint.get(k) or "").lower() for k in ("username", "title")} - {""}
    match = next((a for a in accounts if a.name.lower() in names), None)
    return match.id if match else None


@router.post("/setup/account")
async def step_account_save(_: None = Depends(require_session), account_id: int = Form(...),
                            account_name: str = Form("")):
    plex_client.save_account(
        plex_client.PlexAccountInfo(int(account_id), account_name or str(account_id), None)
    )
    return _redirect("/setup/libraries", f"Only {account_name or account_id}'s viewing will be synced.", "ok")


@router.get("/setup/libraries", response_class=HTMLResponse)
async def step_libraries(request: Request, _: None = Depends(require_session)):
    libraries, error = [], None
    server = plex_client.stored_server()
    if server:
        try:
            libraries = await plex_client.run(PlexClient(*server).libraries_sync)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            error = str(exc)
    enabled = plex_client.enabled_section_ids()
    suggested = not enabled
    if suggested:
        enabled = {lib.section_id for lib in libraries if _ANIME_NAME.search(lib.title)}
    return _page(request, "libraries", libraries=libraries, plex_error=error,
                 enabled_sections=enabled, suggested=suggested and bool(enabled),
                 has_server=server is not None)


@router.post("/setup/libraries")
async def step_libraries_save(request: Request, _: None = Depends(require_session)):
    form = await request.form()
    chosen = {int(v) for v in form.getlist("section_id")}
    if not chosen:
        return _redirect("/setup/libraries", "Tick at least one library.", "error")
    await plex_client.save_libraries_from_server(chosen)
    return _redirect("/setup/anilist", f"{len(chosen)} library(ies) will be watched.", "ok")


# --------------------------------------------------------------------------
# 5. AniList, 6. MyAnimeList
# --------------------------------------------------------------------------
def _provider_page(request: Request, provider: str, **extra: Any) -> HTMLResponse:
    return _page(
        request,
        "anilist" if provider == tokens.ANILIST else "mal",
        client=oauth.get_client(provider),
        redirect_uri=oauth.redirect_uri(_base(request), provider),
        token=tokens.get(provider),
        developer_url=oauth.ANILIST_DEVELOPER if provider == tokens.ANILIST else oauth.MAL_DEVELOPER,
        **extra,
    )


@router.get("/setup/anilist", response_class=HTMLResponse)
async def step_anilist(request: Request, _: None = Depends(require_session)):
    return _provider_page(request, tokens.ANILIST)


@router.get("/setup/mal", response_class=HTMLResponse)
async def step_mal(request: Request, _: None = Depends(require_session)):
    return _provider_page(request, tokens.MAL)


@router.post("/setup/{provider}/client")
async def step_save_client(provider: str, _: None = Depends(require_session),
                           client_id: str = Form(""), client_secret: str = Form("")):
    if provider not in oauth.CALLBACK_PATH:
        return _redirect("/setup", "Unknown provider.", "error")
    try:
        oauth.save_client(provider, client_id, client_secret)
    except oauth.OAuthError as exc:
        return _redirect(f"/setup/{provider}", f"Not saved: {exc}.", "error")
    return _redirect(f"/setup/{provider}", "Client saved. Now sign in.", "ok")


@router.post("/setup/{provider}/start")
async def step_oauth_start(request: Request, provider: str, _: None = Depends(require_session)):
    if provider not in oauth.CALLBACK_PATH:
        return _redirect("/setup", "Unknown provider.", "error")
    client = oauth.get_client(provider)
    if client is None:
        return _redirect(f"/setup/{provider}", "Enter your client id first.", "error")
    redirect = oauth.redirect_uri(_base(request), provider)
    url = (oauth.anilist_authorize_url if provider == tokens.ANILIST else oauth.mal_authorize_url)(
        client, redirect
    )
    return RedirectResponse(url, status_code=303)


@router.get("/setup/anilist/callback")
async def step_anilist_callback(_: None = Depends(require_session), code: str | None = None,
                                state: str | None = None, error: str | None = None):
    if error:
        return _redirect("/setup/anilist", f"AniList sign-in was not completed: {error}.", "error")
    try:
        grant = await oauth.anilist_finish(code=code, state=state)
    except oauth.OAuthError as exc:
        return _redirect("/setup/anilist", f"{exc}.", "error")
    message, level = await connect_anilist(grant.access_token, expires_at=grant.expires_at)
    return _redirect("/setup/mal" if level == "ok" else "/setup/anilist", message, level)


@router.get("/setup/mal/callback")
async def step_mal_callback(_: None = Depends(require_session), code: str | None = None,
                            state: str | None = None, error: str | None = None):
    if error:
        return _redirect("/setup/mal", f"MyAnimeList sign-in was not completed: {error}.", "error")
    try:
        grant = await oauth.mal_finish(code=code, state=state)
        viewer = await oauth.mal_viewer(grant.access_token)
    except oauth.OAuthError as exc:
        return _redirect("/setup/mal", f"{exc}.", "error")
    oauth.store_mal(grant, viewer)
    return _redirect("/setup/webhook", f"Connected to MyAnimeList as {viewer.get('name')}.", "ok")


@router.post("/setup/anilist/token")
async def step_anilist_token(_: None = Depends(require_session), access_token: str = Form(...)):
    message, level = await connect_anilist(access_token)
    return _redirect("/setup/mal" if level == "ok" else "/setup/anilist", message, level)


# --------------------------------------------------------------------------
# 7. webhook, then done
# --------------------------------------------------------------------------
@router.get("/setup/webhook", response_class=HTMLResponse)
async def step_webhook(request: Request, _: None = Depends(require_session)):
    return _page(request, "webhook", webhook_url=_webhook_url(request), last_webhook=_watermark("last_webhook"))


@router.get("/setup/webhook/status")
async def step_webhook_status(_: None = Depends(require_session)) -> JSONResponse:
    last = _watermark("last_webhook")
    return JSONResponse({"received": last is not None, "at": last})


@router.get("/setup/done", response_class=HTMLResponse)
async def step_done(request: Request, _: None = Depends(require_session)):
    return _page(request, "done")
