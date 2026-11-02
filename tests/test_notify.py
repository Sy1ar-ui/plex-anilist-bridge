"""Notifications: what is sent, in what shape, how often, and that it never gets in the way."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, notify, settings_store, tokens
from app.main import create_app
from app.models import AniListEntryState
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event

DISCORD = "https://discord.com/api/webhooks/123/abc"
NTFY = "https://ntfy.sh/my-anime"
GENERIC = "https://hooks.example.net/in"


def make_event(**kw):
    from dataclasses import replace

    return replace(_make_event(**kw), received_at=datetime.now(UTC))


class Hook:
    """Stands in for the webhook endpoint; records what it was sent."""

    def __init__(self, status=200, fail=False):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.fail = fail

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(self.status, text="nope" if self.status >= 400 else "ok")


@pytest.fixture
def hook(monkeypatch):
    fake = Hook()
    real = httpx.AsyncClient

    def factory(*_a, **kw):
        return real(transport=httpx.MockTransport(fake), **kw)

    monkeypatch.setattr(notify.httpx, "AsyncClient", factory)
    return fake


@pytest.fixture
def configured(temp_db, settings):
    settings_store.set_many({"notify_webhook_url": DISCORD, "ui_base_url": "http://bridge.lan:8787"})


def rows(kind=None):
    sql, args = "SELECT * FROM notification_log", ()
    if kind:
        sql, args = sql + " WHERE kind = ?", (kind,)
    return [dict(r) for r in db.query(sql + " ORDER BY id", args)]


# --------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------
MSG = notify.Message(notify.KIND_RATING, "Ready to rate: Frieren", "You finished Frieren.", "http://b/#rate")


def test_the_service_is_worked_out_from_the_url(temp_db):
    assert notify.service(DISCORD) == "discord"
    assert notify.service("https://discordapp.com/api/webhooks/1/x") == "discord"
    assert notify.service(NTFY) == "ntfy"
    assert notify.service("https://ntfy.home.lan/anime") == "ntfy"
    assert notify.service(GENERIC) == "json"
    settings_store.set("notify_service", "ntfy")
    assert notify.service(GENERIC) == "ntfy"


def test_discord_gets_an_embed_and_can_never_ping_anyone():
    body = notify.payload("discord", MSG)["json"]
    assert body["embeds"][0]["title"] == "Ready to rate: Frieren"
    assert body["embeds"][0]["url"] == "http://b/#rate"
    assert body["allowed_mentions"] == {"parse": []}


def test_ntfy_gets_plain_text_with_a_title_and_a_click_link():
    request = notify.payload("ntfy", MSG)
    assert request["content"] == b"You finished Frieren."
    assert request["headers"]["Title"] == "Ready to rate: Frieren"
    assert request["headers"]["Click"] == "http://b/#rate"


def test_ntfy_titles_that_are_not_latin1_still_send():
    message = notify.Message(notify.KIND_RATING, "Ready to rate: 葬送のフリーレン", "x")
    assert notify.payload("ntfy", message)["headers"]["Title"].startswith("Ready to rate: ")


def test_plain_json_carries_everything():
    body = notify.payload("json", MSG)["json"]
    assert body["kind"] == "rating" and body["url"] == "http://b/#rate"
    assert "You finished Frieren." in body["text"]


# --------------------------------------------------------------------------
# when, and how often
# --------------------------------------------------------------------------
def test_nothing_is_queued_without_a_url(temp_db, settings):
    assert notify.notify(MSG) is None
    assert rows() == []


def test_a_kind_can_be_switched_off(configured, hook):
    settings_store.set("notify_on_rating", False)
    assert notify.notify(MSG) is None


def test_the_same_thing_is_sent_once(configured, hook):
    assert notify.notify(MSG, key="rating:1") is not None
    assert notify.notify(MSG, key="rating:1") is None
    assert notify.notify(MSG, key="rating:2") is not None


def test_a_failed_send_does_not_count_as_sent(configured, hook):
    hook.status = 500
    log_id = notify.notify(MSG, key="rating:1")
    assert rows()[0]["result"] == "failed" and "HTTP 500" in rows()[0]["error"]
    assert notify.notify(MSG, key="rating:1") not in (None, log_id)   # tried again


def test_delivery_is_recorded(configured, hook):
    notify.notify(MSG)
    assert rows()[0]["result"] == "sent"
    sent = json.loads(hook.requests[0].content)
    assert sent["embeds"][0]["title"] == "Ready to rate: Frieren"


def test_an_unreachable_webhook_never_raises(configured, hook):
    hook.fail = True
    assert notify.notify(MSG) is not None
    assert "could not reach" in rows()[0]["error"]


def test_links_point_at_the_ui(configured):
    assert notify.link("/review/5") == "http://bridge.lan:8787/review/5"
    settings_store.set("ui_base_url", "")
    assert notify.link("/review/5") is None


def test_sending_in_the_background_does_not_hold_up_the_caller(configured, hook):
    async def go():
        notify.notify(MSG)
        queued = rows()[0]["result"]
        await notify.drain()
        return queued, rows()[0]["result"]

    assert asyncio.run(go()) == ("pending", "sent")


# --------------------------------------------------------------------------
# what raises them
# --------------------------------------------------------------------------
def test_one_review_notification_per_show_and_season(datasets, configured, hook):
    client = FakeAniList(AniListEntryState(anilist_id=1, exists=False))
    for episode in (1, 2, 3):
        asyncio.run(handle_watch_event(
            make_event(tvdb_id=9_999_001, plex_guid="plex://show/x", title="Obscure Show",
                       season=1, episode=episode, rating_key=f"rk{episode}"),
            anilist=client, gate=WriteGate(dry_run=True)))
    [review] = rows("review")
    assert review["title"] == "Needs review: Obscure Show season 1"
    assert review["link"].startswith("http://bridge.lan:8787/review/")


def test_a_finished_series_asks_for_its_rating(datasets, configured, hook):
    state = AniListEntryState(anilist_id=290, exists=True, status="CURRENT", progress=12,
                              total_episodes=13, media_status="FINISHED", title="Crest of the Stars")
    for key in ("a", "b"):   # watching the finale twice still asks once
        asyncio.run(handle_watch_event(
            make_event(tvdb_id=72025, season=1, episode=13, rating_key=key),
            anilist=FakeAniList(state), gate=WriteGate(dry_run=True)))
    [rating] = rows("rating")
    assert rating["title"] == "Ready to rate: Crest of the Stars"
    assert "12/13" in rating["body"]


def test_a_rejected_token_is_reported_once(configured, hook):
    tokens.save(tokens.ANILIST, access_token="t", account_name="me")
    tokens.mark_broken(tokens.ANILIST, "Invalid token")
    tokens.mark_broken(tokens.ANILIST, "Invalid token")
    [token] = rows("token")
    assert token["title"] == "AniList needs you to sign in again"
    assert token["link"] == "http://bridge.lan:8787/setup/anilist"


def test_other_accounts_never_cause_a_notification(configured, hook):
    """Another account's play is dropped by the filter before anything could notify."""
    from pathlib import Path

    from app import auth
    from app.plex import client as plex_client

    plex_client.save_account(plex_client.PlexAccountInfo(1, "me", None))
    theirs = (Path(__file__).parent / "fixtures" / "webhook_scrobble_other_account.json").read_text()
    response = TestClient(create_app()).post(
        f"/webhook/plex/{auth.webhook_path_token()}", files={"payload": (None, theirs)})
    assert response.status_code < 300
    assert rows() == [] and hook.requests == []


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------
@pytest.fixture
def ui(datasets, settings):
    settings_store.set("setup_complete", True)
    return TestClient(create_app(), base_url="http://bridge.lan:8787")


def test_the_settings_save_the_switches(ui):
    ui.post("/settings/behaviour", data={"dry_run": "on", "notify_webhook_url": NTFY, "notify_service": "ntfy",
                                         "notify_on_rating": "on"}, follow_redirects=False)
    assert settings_store.get("notify_webhook_url") == NTFY
    assert settings_store.get("notify_service") == "ntfy"
    assert settings_store.get("notify_on_rating") is True
    assert settings_store.get("notify_on_review") is False


def test_send_a_test_uses_the_url_in_the_form(ui, hook):
    response = ui.post("/settings/notify-test", data={"notify_webhook_url": NTFY, "notify_service": "auto"},
                       follow_redirects=False)
    assert "Test+sent+as+ntfy" in response.headers["location"]
    assert str(hook.requests[0].url) == NTFY
    assert settings_store.get("notify_webhook_url") == ""     # testing does not save


def test_a_failed_test_says_why(ui, hook):
    hook.status = 404
    response = ui.post("/settings/notify-test", data={"notify_webhook_url": GENERIC}, follow_redirects=False)
    assert "level=error" in response.headers["location"] and "HTTP+404" in response.headers["location"]


def test_a_test_needs_a_web_url(ui):
    response = ui.post("/settings/notify-test", data={"notify_webhook_url": "file:///etc/passwd"},
                       follow_redirects=False)
    assert "level=error" in response.headers["location"]


def test_the_ui_address_is_remembered_for_links(ui):
    ui.get("/settings")
    assert settings_store.get("ui_base_url") == "http://bridge.lan:8787"


def test_recent_sends_are_shown_in_settings(ui, hook):
    settings_store.set("notify_webhook_url", DISCORD)
    notify.notify(MSG)
    page = ui.get("/settings").text
    assert 'id="notifications"' in page and "Ready to rate: Frieren" in page
