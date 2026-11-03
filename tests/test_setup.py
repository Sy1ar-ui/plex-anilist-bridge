"""The setup wizard: Plex PIN sign-in, server discovery, AniList and MAL OAuth.

No network: plex.tv, the Plex server's /identity, AniList and MyAnimeList are
answered by an httpx MockTransport, and plexapi calls are stubbed.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, oauth, settings_store, tokens
from app import setup as setup_state
from app.main import create_app
from app.plex import account as plex_account
from app.plex import client as plex_client

SERVER_ID = "abc123machine"
ACCOUNT_TOKEN = "account-token-xyz"
SERVER_TOKEN = "server-token-123"

RESOURCES = [
    {
        "name": "Shed Server", "provides": "server", "clientIdentifier": SERVER_ID,
        "owned": True, "accessToken": SERVER_TOKEN,
        "connections": [
            {"protocol": "https", "address": "203.0.113.5", "port": 32400, "local": False, "relay": False,
             "uri": "https://203-0-113-5.abc123.plex.direct:32400"},
            {"protocol": "https", "address": "192.168.1.10", "port": 32400, "local": True, "relay": False,
             "uri": "https://192-168-1-10.abc123.plex.direct:32400"},
            {"protocol": "https", "address": "relay", "port": 443, "local": False, "relay": True,
             "uri": "https://relay.plex.direct:443"},
        ],
    },
    {"name": "A Phone", "provides": "client,player", "clientIdentifier": "phone", "connections": []},
    {"name": "Friend's Server", "provides": "server", "clientIdentifier": "friend", "owned": False,
     "accessToken": "friend-token", "connections": []},
]


class FakeNet:
    """Answers for plex.tv, the Plex server, AniList and MAL; records requests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.pin_token: str | None = None
        self.working = {"http://192.168.1.10:32400"}   # what answers from "this container"
        self.anilist_token_status = 200
        self.mal_token = {"access_token": "mal-access", "refresh_token": "mal-refresh", "expires_in": 2678400}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        host = request.url.host
        if host == "plex.tv":
            path = request.url.path
            if request.method == "POST" and path == "/api/v2/pins":
                return httpx.Response(201, json={"id": 4242, "code": "pincode"})
            if path == "/api/v2/pins/4242":
                return httpx.Response(200, json={"id": 4242, "authToken": self.pin_token})
            if path.startswith("/api/v2/pins/"):
                return httpx.Response(404, json={})
            if path == "/api/v2/user":
                return httpx.Response(200, json={"id": 99, "username": "jay", "title": "Jay"})
            if path == "/api/v2/resources":
                return httpx.Response(200, json=RESOURCES)
        if request.url.path == "/identity":
            base = f"{request.url.scheme}://{host}:{request.url.port}"
            if base in self.working:
                return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": SERVER_ID}})
            raise httpx.ConnectError("connection refused", request=request)
        if url == oauth.ANILIST_TOKEN:
            if self.anilist_token_status != 200:
                return httpx.Response(self.anilist_token_status, json={"error": "invalid_client",
                                                                       "hint": "Client authentication failed"})
            return httpx.Response(200, json={"access_token": "anilist-access", "expires_in": 31536000,
                                             "token_type": "Bearer"})
        if url == oauth.MAL_TOKEN:
            return httpx.Response(200, json=self.mal_token)
        if url == oauth.MAL_ME:
            return httpx.Response(200, json={"id": 555, "name": "jay_mal"})
        return httpx.Response(599, text=f"unexpected request {request.method} {url}")


@pytest.fixture
def net(monkeypatch):
    """Route every httpx.AsyncClient made by the setup modules through FakeNet.

    The modules look up ``httpx.AsyncClient`` when they call it, so this patches
    the shared httpx module for the length of the test.
    """
    fake = FakeNet()
    real = httpx.AsyncClient

    def factory(*_args, **kwargs):
        kwargs.pop("verify", None)
        return real(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(plex_account.httpx, "AsyncClient", factory)
    monkeypatch.setattr(oauth.httpx, "AsyncClient", factory)
    return fake


# --------------------------------------------------------------------------
# plex.tv
# --------------------------------------------------------------------------
def test_a_pin_is_created_and_the_auth_url_carries_it(temp_db, net):
    pin = asyncio.run(plex_account.create_pin())
    assert pin == plex_account.Pin(4242, "pincode")
    sent = net.requests[0]
    assert sent.url.params["strong"] == "true"
    assert sent.headers["X-Plex-Client-Identifier"] == plex_account.client_identifier()
    assert sent.headers["X-Plex-Product"] == plex_account.PRODUCT

    url = plex_account.auth_url(pin, "http://bridge:8787/setup/plex/return?n=abc")
    assert url.startswith("https://app.plex.tv/auth#?")
    fragment = parse_qs(url.split("#?", 1)[1])
    assert fragment["code"] == ["pincode"]
    assert fragment["forwardUrl"] == ["http://bridge:8787/setup/plex/return?n=abc"]
    assert fragment["clientID"] == [plex_account.client_identifier()]


def test_the_client_identifier_is_stable(temp_db):
    assert plex_account.client_identifier() == plex_account.client_identifier()


def test_a_pin_has_no_token_until_the_user_signs_in(temp_db, net):
    assert asyncio.run(plex_account.check_pin(4242)) is None
    net.pin_token = ACCOUNT_TOKEN
    assert asyncio.run(plex_account.check_pin(4242)) == ACCOUNT_TOKEN


def test_an_expired_pin_says_so(temp_db, net):
    with pytest.raises(plex_account.PlexAuthError, match="expired"):
        asyncio.run(plex_account.check_pin(1))


def test_only_servers_are_listed_with_your_own_first(temp_db, net):
    servers = asyncio.run(plex_account.servers(ACCOUNT_TOKEN))
    assert [s.name for s in servers] == ["Shed Server", "Friend's Server"]
    assert servers[0].access_token == SERVER_TOKEN


def test_addresses_are_ordered_local_first_relay_last_with_a_plain_http_fallback(temp_db, net):
    [shed, _] = asyncio.run(plex_account.servers(ACCOUNT_TOKEN))
    uris = [c.uri for c in shed.connections]
    assert uris == [
        "https://192-168-1-10.abc123.plex.direct:32400",
        "http://192.168.1.10:32400",
        "https://203-0-113-5.abc123.plex.direct:32400",
        "https://relay.plex.direct:443",
    ]


def test_probing_marks_what_answers_from_here(temp_db, net):
    [shed, _] = asyncio.run(plex_account.servers(ACCOUNT_TOKEN))
    asyncio.run(plex_account.probe(shed))
    status = {c.uri: c.ok for c in shed.connections}
    assert status["http://192.168.1.10:32400"] is True
    assert status["https://192-168-1-10.abc123.plex.direct:32400"] is False
    assert shed.best.uri == "http://192.168.1.10:32400"
    # The probe sends the server's token, not the account's.
    identity = [r for r in net.requests if r.url.path == "/identity"]
    assert {r.headers["X-Plex-Token"] for r in identity} == {SERVER_TOKEN}


def test_a_different_server_at_the_address_is_not_accepted(temp_db, net):
    [shed, _] = asyncio.run(plex_account.servers(ACCOUNT_TOKEN))
    shed.client_id = "someone-else"
    asyncio.run(plex_account.probe(shed))
    assert shed.best is None
    assert any("different Plex server" in (c.error or "") for c in shed.connections)


# --------------------------------------------------------------------------
# OAuth state and clients
# --------------------------------------------------------------------------
def test_anilist_client_ids_are_validated(temp_db):
    with pytest.raises(oauth.OAuthError, match="number"):
        oauth.save_client(tokens.ANILIST, "abc", "secret")
    with pytest.raises(oauth.OAuthError, match="secret"):
        oauth.save_client(tokens.ANILIST, "123", "")
    oauth.save_client(tokens.ANILIST, "123", "secret")
    # Saving again with a blank secret keeps the stored one.
    assert oauth.save_client(tokens.ANILIST, "123", "").client_secret == "secret"


def test_a_mal_client_may_have_no_secret(temp_db):
    assert oauth.save_client(tokens.MAL, "malid", "").client_secret is None


def test_the_anilist_authorize_url(temp_db):
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    url = urlparse(oauth.anilist_authorize_url(client, "http://bridge/setup/anilist/callback"))
    query = parse_qs(url.query)
    assert url.netloc == "anilist.co"
    assert query["client_id"] == ["123"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["http://bridge/setup/anilist/callback"]
    assert len(query["state"][0]) >= 24


def test_a_state_works_once(temp_db, net):
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    state = parse_qs(urlparse(oauth.anilist_authorize_url(client, "http://b/cb")).query)["state"][0]
    asyncio.run(oauth.anilist_finish(code="c", state=state))
    with pytest.raises(oauth.OAuthError, match="already used"):
        asyncio.run(oauth.anilist_finish(code="c", state=state))


def test_a_forged_state_is_refused(temp_db, net):
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    oauth.anilist_authorize_url(client, "http://b/cb")
    with pytest.raises(oauth.OAuthError):
        asyncio.run(oauth.anilist_finish(code="c", state="not-the-state"))
    assert not [r for r in net.requests if str(r.url) == oauth.ANILIST_TOKEN]


def test_a_stale_state_is_refused(temp_db, net, monkeypatch):
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    state = parse_qs(urlparse(oauth.anilist_authorize_url(client, "http://b/cb")).query)["state"][0]
    later = time.time() + oauth.STATE_TTL_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    with pytest.raises(oauth.OAuthError, match="expired"):
        asyncio.run(oauth.anilist_finish(code="c", state=state))


def test_the_anilist_code_exchange(temp_db, net):
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    state = parse_qs(urlparse(oauth.anilist_authorize_url(client, "http://b/cb")).query)["state"][0]
    grant = asyncio.run(oauth.anilist_finish(code="the-code", state=state))
    assert grant.access_token == "anilist-access"
    assert grant.expires_at is not None
    [sent] = [r for r in net.requests if str(r.url) == oauth.ANILIST_TOKEN]
    assert json.loads(sent.content) == {
        "grant_type": "authorization_code", "client_id": "123", "client_secret": "secret",
        "redirect_uri": "http://b/cb", "code": "the-code",
    }


def test_a_refused_exchange_explains_itself(temp_db, net):
    net.anilist_token_status = 401
    client = oauth.save_client(tokens.ANILIST, "123", "secret")
    state = parse_qs(urlparse(oauth.anilist_authorize_url(client, "http://b/cb")).query)["state"][0]
    with pytest.raises(oauth.OAuthError, match="Client authentication failed.*redirect URL"):
        asyncio.run(oauth.anilist_finish(code="c", state=state))


def test_mal_uses_a_plain_pkce_verifier_kept_on_the_server(temp_db, net):
    client = oauth.save_client(tokens.MAL, "malid", "")
    query = parse_qs(urlparse(oauth.mal_authorize_url(client, "http://b/mcb")).query)
    challenge = query["code_challenge"][0]
    assert query["code_challenge_method"] == ["plain"]
    assert 43 <= len(challenge) <= 128

    grant = asyncio.run(oauth.mal_finish(code="mc", state=query["state"][0]))
    assert grant.refresh_token == "mal-refresh"
    [sent] = [r for r in net.requests if str(r.url) == oauth.MAL_TOKEN]
    form = parse_qs(sent.content.decode())
    assert form["code_verifier"] == [challenge]
    assert form["grant_type"] == ["authorization_code"]
    assert "client_secret" not in form


# --------------------------------------------------------------------------
# step state
# --------------------------------------------------------------------------
def test_steps_start_with_the_password_and_mal_is_optional(temp_db):
    steps = setup_state.steps()
    assert [s.key for s in steps] == list(setup_state.ORDER)
    assert setup_state.next_step().key == "password"
    assert [s.key for s in steps if s.optional] == ["mal"]
    assert setup_state.is_fresh_install()


# --------------------------------------------------------------------------
# the wizard, through HTTP
# --------------------------------------------------------------------------
@pytest.fixture
def wizard(datasets, settings, net, monkeypatch):
    from app.web import routes_ui

    class Viewer:
        def __init__(self, token, **_kw):
            self.token = token

        async def viewer(self):
            return {"name": "jay_anilist", "id": 7}

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(routes_ui, "AniListClient", Viewer)
    monkeypatch.setattr(plex_client.PlexClient, "check_sync",
                        lambda self: {"name": "Shed Server", "machine_id": SERVER_ID})
    monkeypatch.setattr(plex_client.PlexClient, "accounts_sync", lambda self: [
        plex_client.PlexAccountInfo(1, "jay", None), plex_client.PlexAccountInfo(812, "lodger", None)])
    monkeypatch.setattr(plex_client.PlexClient, "libraries_sync", lambda self: [
        plex_client.PlexLibraryInfo(1, "Movies", "movie"), plex_client.PlexLibraryInfo(2, "Anime", "show"),
        plex_client.PlexLibraryInfo(3, "TV Shows", "show")])
    return TestClient(create_app(), base_url="http://bridge.lan:8787")


def location(response) -> str:
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_the_wizard_starts_at_the_password(wizard):
    assert location(wizard.get("/setup", follow_redirects=False)) == "/setup/password"
    page = wizard.get("/setup/password")
    assert page.status_code == 200
    assert 'aria-current="step"' in page.text


def test_mismatched_passwords_are_refused(wizard):
    response = wizard.post("/setup/password", data={"password": "longenough", "confirm": "different"},
                           follow_redirects=False)
    assert "level=error" in location(response)


def test_plex_sign_in_from_start_to_a_saved_server(wizard, net):
    wizard.post("/setup/password", data={"password": "longenough", "confirm": "longenough"})

    start = location(wizard.post("/setup/plex/start", follow_redirects=False))
    assert start.startswith("https://app.plex.tv/auth#?")
    forward = parse_qs(start.split("#?", 1)[1])["forwardUrl"][0]
    assert forward.startswith("http://bridge.lan:8787/setup/plex/return?n=")
    return_path = forward.replace("http://bridge.lan:8787", "")

    # Back before Plex confirmed: told to wait, nothing stored.
    assert "not+confirmed" in location(wizard.get(return_path, follow_redirects=False))
    assert "Finish signing in" in wizard.get("/setup/plex").text

    net.pin_token = ACCOUNT_TOKEN
    assert "Signed+in+to+Plex+as+Jay" in location(wizard.get(return_path, follow_redirects=False))

    page = wizard.get("/setup/plex")
    assert "Shed Server" in page.text
    assert "http://192.168.1.10:32400" in page.text
    assert "works" in page.text

    done = wizard.post("/setup/plex/choose", data={"server_id": SERVER_ID, "uri": "http://192.168.1.10:32400"},
                       follow_redirects=False)
    assert location(done).startswith("/setup/account")
    assert plex_client.stored_server() == ("http://192.168.1.10:32400", SERVER_TOKEN)
    # The account token is not kept anywhere once the server is chosen.
    for row in db.query("SELECT value_json FROM setting"):
        assert ACCOUNT_TOKEN not in row["value_json"]


def test_a_return_with_the_wrong_nonce_is_refused(wizard, net):
    wizard.post("/setup/plex/start", follow_redirects=False)
    net.pin_token = ACCOUNT_TOKEN
    response = wizard.get("/setup/plex/return?n=forged", follow_redirects=False)
    assert "level=error" in location(response)
    assert not plex_account.pending().get("token")


def test_only_an_address_plex_listed_can_be_chosen(wizard, net):
    plex_account.remember_token(ACCOUNT_TOKEN, plex_account.PlexUser(99, "jay", "Jay"))
    response = wizard.post("/setup/plex/choose", data={"server_id": SERVER_ID, "uri": "http://evil.example:80"},
                           follow_redirects=False)
    assert "level=error" in location(response)
    assert plex_client.stored_server() is None


def test_the_owner_is_preselected_and_anime_libraries_are_suggested(wizard):
    plex_client.save_server("http://192.168.1.10:32400", SERVER_TOKEN, name="Shed", machine_id=SERVER_ID)
    settings_store.set("plex_signed_in_as", {"username": "jay", "title": "Jay", "owned": True})
    account = wizard.get("/setup/account").text
    assert 'value="1" data-name="jay" required\n               checked' in account

    libraries = wizard.get("/setup/libraries").text
    assert 'value="2"\n               checked' in libraries     # "Anime"
    assert 'value="1"\n               checked' not in libraries  # "Movies"
    assert "a guess from their names" in libraries

    response = wizard.post("/setup/libraries", data={"section_id": ["2"]}, follow_redirects=False)
    assert location(response).startswith("/setup/anilist")
    assert plex_client.enabled_section_ids() == {2}


def test_anilist_oauth_through_the_wizard(wizard, net):
    page = wizard.get("/setup/anilist").text
    assert "http://bridge.lan:8787/setup/anilist/callback" in page   # the URL to register

    wizard.post("/setup/anilist/client", data={"client_id": "123", "client_secret": "secret"})
    authorize = location(wizard.post("/setup/anilist/start", follow_redirects=False))
    state = parse_qs(urlparse(authorize).query)["state"][0]

    back = wizard.get(f"/setup/anilist/callback?code=the-code&state={state}", follow_redirects=False)
    assert location(back).startswith("/setup/mal")
    record = tokens.get(tokens.ANILIST)
    assert record.access_token == "anilist-access"
    assert record.account_name == "jay_anilist"
    assert record.expires_at is not None


def test_a_declined_anilist_sign_in_stores_nothing(wizard):
    back = wizard.get("/setup/anilist/callback?error=access_denied", follow_redirects=False)
    assert "level=error" in location(back)
    assert not tokens.get(tokens.ANILIST).present


def test_mal_oauth_through_the_wizard(wizard, net):
    wizard.post("/setup/mal/client", data={"client_id": "malid"})
    authorize = location(wizard.post("/setup/mal/start", follow_redirects=False))
    state = parse_qs(urlparse(authorize).query)["state"][0]
    back = wizard.get(f"/setup/mal/callback?code=mc&state={state}", follow_redirects=False)
    assert location(back).startswith("/setup/webhook")
    record = tokens.get(tokens.MAL)
    assert (record.access_token, record.refresh_token, record.account_name) == ("mal-access", "mal-refresh", "jay_mal")


def test_the_webhook_step_reports_arrival(wizard):
    assert wizard.get("/setup/webhook/status").json() == {"received": False, "at": None, "foreign": 0, "other_server": 0}
    db.execute("INSERT INTO watermark (name, value) VALUES ('last_webhook', datetime('now'))")
    assert wizard.get("/setup/webhook/status").json()["received"] is True
    assert "this step is done" in wizard.get("/setup/webhook").text


def test_the_webhook_step_says_why_deliveries_were_dropped(wizard):
    from app.web.routes_webhook import COUNTER_FOREIGN, COUNTER_OTHER_SERVER

    page = wizard.get("/setup/webhook").text
    assert 'id="webhook-other-server" class="callout tone-warn" role="status" hidden' in page
    assert 'id="webhook-foreign" class="callout tone-info" role="status" hidden' in page

    db.bump_counter(COUNTER_FOREIGN)
    db.bump_counter(COUNTER_OTHER_SERVER)
    status = wizard.get("/setup/webhook/status").json()
    assert (status["foreign"], status["other_server"]) == (1, 1)
    page = wizard.get("/setup/webhook").text
    assert 'role="status" hidden' not in page
    assert "sent by a different Plex server" in page
    assert "from other accounts on" in page


def test_finishing_sends_you_to_the_dashboard_from_then_on(wizard):
    assert location(wizard.get("/", follow_redirects=False)) == "/setup"
    wizard.post("/setup/finish")
    assert wizard.get("/", follow_redirects=False).status_code == 200


def test_every_step_page_renders(wizard):
    for key in (*setup_state.ORDER, "done"):
        page = wizard.get(f"/setup/{key}")
        assert page.status_code == 200, key
        assert "Setup" in page.text
