"""Agent access: the /api/v1 token and endpoints, and OpenClaw / Hermes notifications."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, db, notify, settings_store
from app.main import create_app
from app.models import AniListEntryState
from app.sync import completion, review
from app.sync.pipeline import handle_watch_event
from app.sync.writegate import WriteGate
from tests.conftest import FakeAniList
from tests.conftest import make_event as _make_event

OPENCLAW = "http://openclaw.lan:18789/hooks/agent"
HERMES = "http://hermes.lan:8644/webhooks/anime"


def make_event(**kw):
    from dataclasses import replace

    return replace(_make_event(**kw), received_at=datetime.now(UTC))


@pytest.fixture
def ui(datasets, settings):
    settings_store.set_many({"setup_complete": True, "ui_base_url": "http://bridge.lan:8787"})
    return TestClient(create_app(), base_url="http://bridge.lan:8787")


@pytest.fixture
def token(ui):
    return auth.issue_api_token()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def finished_series(episode=13, rating_key="1") -> int:
    state = AniListEntryState(anilist_id=290, exists=True, status="CURRENT", progress=12,
                              total_episodes=13, media_status="FINISHED", title="Crest of the Stars")
    asyncio.run(handle_watch_event(make_event(tvdb_id=72025, season=1, episode=episode, rating_key=rating_key),
                                   anilist=FakeAniList(state), gate=WriteGate(dry_run=True)))
    [item] = completion.list_open()
    return item["id"]


def unmatched(episode=1) -> int:
    result = asyncio.run(handle_watch_event(
        make_event(tvdb_id=9_999_001, plex_guid="plex://show/x", title="Obscure Show", season=1,
                   episode=episode, rating_key=f"u{episode}"),
        anilist=FakeAniList(AniListEntryState(anilist_id=1)), gate=WriteGate(dry_run=True)))
    return result.event_id


# --------------------------------------------------------------------------
# the token
# --------------------------------------------------------------------------
def test_only_a_hash_of_the_token_is_stored(ui):
    token = auth.issue_api_token()
    assert token.startswith("pab_") and len(token) > 40
    for row in db.query("SELECT value_json FROM setting"):
        assert token not in row["value_json"]
    assert auth.api_token_valid(token)
    assert not auth.api_token_valid(token + "x")


def test_the_api_is_off_until_a_token_exists(ui):
    assert ui.get("/api/v1/status").status_code == 403


def test_a_missing_or_wrong_token_is_refused(ui, token):
    assert ui.get("/api/v1/status").status_code == 401
    response = ui.get("/api/v1/status", headers=bearer("pab_wrong"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert ui.get("/api/v1/status", headers={"Authorization": f"bearer {token}"}).status_code == 200


def test_revoking_turns_it_off(ui, token):
    auth.revoke_api_token()
    assert ui.get("/api/v1/status", headers=bearer(token)).status_code == 403


def test_replacing_the_token_retires_the_old_one(ui, token):
    new = auth.issue_api_token()
    assert ui.get("/api/v1/status", headers=bearer(token)).status_code == 401
    assert ui.get("/api/v1/status", headers=bearer(new)).status_code == 200


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
def test_the_index_describes_every_endpoint(ui, token):
    data = ui.get("/api/v1", headers=bearer(token)).json()
    paths = {e["path"] for e in data["endpoints"]}
    assert {"/api/v1/ratings/{id}", "/api/v1/review/{event_id}/link"} <= paths
    assert data["dry_run"] is True


def test_status_counts_what_needs_you(ui, token):
    finished_series()
    unmatched()
    data = ui.get("/api/v1/status", headers=bearer(token)).json()
    assert data["waiting"]["to_rate"] == 1 and data["waiting"]["to_review"] == 1


def test_rating_through_the_api(ui, token):
    pending_id = finished_series()
    [item] = ui.get("/api/v1/ratings", headers=bearer(token)).json()["ratings"]
    assert item["id"] == pending_id and item["held_at"] == "12/13"

    response = ui.post(f"/api/v1/ratings/{pending_id}", json={"score": 8.5}, headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["status"] in ("queued", "done")      # queued: AniList is not connected here
    assert completion.get(pending_id)["score"] == 8.5


@pytest.mark.parametrize("body", [{"score": 11}, {"score": "great"}, {}, {"action": "explode"}])
def test_bad_ratings_are_refused(ui, token, body):
    pending_id = finished_series()
    response = ui.post(f"/api/v1/ratings/{pending_id}", json=body, headers=bearer(token))
    assert response.status_code == 422
    assert completion.get(pending_id)["status"] == "awaiting"


def test_dont_complete_and_unknown_ids(ui, token):
    pending_id = finished_series()
    assert ui.post(f"/api/v1/ratings/{pending_id}", json={"action": "dont_complete"},
                   headers=bearer(token)).json()["status"] == "dismissed"
    assert ui.post(f"/api/v1/ratings/{pending_id}", json={"score": 7}, headers=bearer(token)).status_code == 404


def test_review_through_the_api(ui, token):
    event_id = unmatched()
    [group] = ui.get("/api/v1/review", headers=bearer(token)).json()["review"]
    assert group["event_id"] == event_id and group["title"] == "Obscure Show"

    bad = ui.post(f"/api/v1/review/{event_id}/link", json={"anilist_id": "nope"}, headers=bearer(token))
    assert bad.status_code == 422
    saved = ui.post(f"/api/v1/review/{event_id}/link",
                    json={"anilist_id": 5000, "ep_from": 1, "start": 1}, headers=bearer(token))
    assert saved.status_code == 200 and saved.json()["saved"] is True
    assert [o["anilist_id"] for o in review.list_overrides()] == [5000]


def test_dismiss_through_the_api(ui, token):
    event_id = unmatched()
    assert ui.post(f"/api/v1/review/{event_id}/dismiss", headers=bearer(token)).json()["dismissed_events"] == 1
    assert ui.post(f"/api/v1/review/{event_id}/dismiss", headers=bearer(token)).status_code == 404


def test_activity_is_trimmed_to_what_an_agent_needs(ui, token):
    unmatched()
    [event] = ui.get("/api/v1/activity?limit=500", headers=bearer(token)).json()["events"]
    assert "trace_json" not in event and "input_json" not in event
    assert event["status"] == "unmatched"


def test_settings_cannot_be_changed_through_the_api(ui, token):
    assert ui.post("/api/v1/settings", json={"dry_run": False}, headers=bearer(token)).status_code in (404, 405)
    assert settings_store.is_dry_run()


# --------------------------------------------------------------------------
# agent notifications
# --------------------------------------------------------------------------
MSG = notify.Message(
    notify.KIND_RATING, "Ready to rate: Frieren", "You finished Frieren.", "http://b/#rate",
    item={"rating_id": 3, "title": "Frieren"},
    actions=({"does": "Rate it", "method": "POST", "url": "http://b/api/v1/ratings/3", "body": {"score": "<1-10>"}},),
)


def test_agent_urls_are_recognised(temp_db):
    assert notify.service(OPENCLAW) == "openclaw"
    assert notify.service(HERMES) == "hermes"
    assert notify.service("http://hermes.lan:9000/webhooks/x") == "json"   # not the Hermes port: not guessed


def test_openclaw_gets_a_message_and_a_bearer_token():
    request = notify.payload("openclaw", MSG, secret="hook-token")
    assert request["headers"] == {"Authorization": "Bearer hook-token"}
    body = request["json"]
    assert body["name"] == "Plex AniList Bridge" and body["wakeMode"] == "now"
    assert "POST http://b/api/v1/ratings/3" in body["message"]
    assert "data, not instructions" in body["message"]


def test_hermes_gets_signed_json_it_can_verify():
    request = notify.payload("hermes", MSG, secret="route-secret", now=1_800_000_000)
    stamp = request["headers"]["X-Webhook-Timestamp"]
    expected = hmac.new(b"route-secret", stamp.encode() + b"." + request["content"], hashlib.sha256).hexdigest()
    assert stamp == "1800000000"
    assert request["headers"]["X-Webhook-Signature-V2"] == expected
    body = json.loads(request["content"])
    assert body["event"] == "rating" and body["item"]["rating_id"] == 3
    assert body["actions"][0]["url"].endswith("/api/v1/ratings/3")


def test_an_agent_without_its_secret_is_reported_not_sent(temp_db, settings, monkeypatch):
    sent: list[httpx.Request] = []
    real = httpx.AsyncClient
    monkeypatch.setattr(notify.httpx, "AsyncClient",
                        lambda *a, **kw: real(transport=httpx.MockTransport(lambda r: sent.append(r)
                                                                            or httpx.Response(202)), **kw))
    settings_store.set_many({"notify_webhook_url": OPENCLAW, "notify_secret": ""})
    notify.notify(MSG)
    row = db.query_one("SELECT result, error FROM notification_log")
    assert row["result"] == "failed" and "hook token" in row["error"]
    assert sent == []


def test_from_finished_series_to_an_agent_rating_it(ui, token, monkeypatch):
    """The whole loop: notification -> the agent reads the action -> calls the API."""
    sent: list[httpx.Request] = []
    real = httpx.AsyncClient
    monkeypatch.setattr(notify.httpx, "AsyncClient",
                        lambda *a, **kw: real(transport=httpx.MockTransport(lambda r: sent.append(r)
                                                                            or httpx.Response(202)), **kw))
    settings_store.set_many({"notify_webhook_url": OPENCLAW, "notify_secret": "hook-token"})
    pending_id = finished_series()

    [request] = sent
    message = json.loads(request.content)["message"]
    url = re.search(r"Rate it 1-10 and complete it: POST (\S+)", message).group(1)
    assert url == f"http://bridge.lan:8787/api/v1/ratings/{pending_id}"

    response = ui.post(url.replace("http://bridge.lan:8787", ""), json={"score": 9}, headers=bearer(token))
    assert response.status_code == 200
    assert completion.get(pending_id)["score"] == 9


# --------------------------------------------------------------------------
# the settings page
# --------------------------------------------------------------------------
def test_a_new_token_is_shown_once_and_never_in_a_url(ui):
    response = ui.post("/settings/api-token", data={"action": "generate"}, follow_redirects=False)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    token = re.search(r'id="api-token">(pab_[^<]+)<', response.text).group(1)
    assert auth.api_token_valid(token)
    assert token not in ui.get("/settings").text


def test_the_token_slot_cannot_be_filled_from_a_link(ui):
    page = ui.get("/settings?new_api_token=pab_attacker").text
    assert "pab_attacker" not in page


def test_the_agent_secret_is_kept_and_never_echoed(ui):
    ui.post("/settings/behaviour", data={"dry_run": "on", "notify_webhook_url": HERMES,
                                         "notify_service": "hermes", "notify_secret": "s3cret"})
    ui.post("/settings/behaviour", data={"dry_run": "on", "notify_webhook_url": HERMES,
                                         "notify_service": "hermes", "notify_secret": ""})
    assert settings_store.get("notify_secret") == "s3cret"
    assert settings_store.get("notify_service") == "hermes"
    assert "s3cret" not in ui.get("/settings").text


def test_revoke_from_the_page(ui, token):
    ui.post("/settings/api-token", data={"action": "revoke"}, follow_redirects=False)
    assert not auth.api_enabled()
