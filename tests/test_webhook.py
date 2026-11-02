"""Webhook parsing and -- the important one -- the account filter.

The Plex server is shared. The requirement is not merely that another user's
events are ignored, but that nothing about what they watched is processed or
logged at all. That is asserted here by checking the database contains no trace
of the other user's show title.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, db
from app.plex import client as plex_client
from app.plex import webhook
from app.plex.client import PlexAccountInfo
from app.web import routes_webhook
from tests.conftest import FIXTURES

MINE = json.loads((FIXTURES / "webhook_scrobble_episode.json").read_text())
THEIRS = json.loads((FIXTURES / "webhook_scrobble_other_account.json").read_text())
PAUSE = json.loads((FIXTURES / "webhook_pause.json").read_text())

FOREIGN_TITLE = "Someone Elses Private Show"
FOREIGN_EPISODE_TITLE = "A Secret Episode Title"


# --------------------------------------------------------------------------
# pure parsing
# --------------------------------------------------------------------------
def test_account_id_is_read_from_the_payload():
    assert webhook.account_id_of(MINE) == 1
    assert webhook.account_id_of(THEIRS) == 77


def test_account_id_handles_a_string_and_rejects_nonsense():
    assert webhook.account_id_of({"Account": {"id": "12"}}) == 12
    assert webhook.account_id_of({"Account": {"id": True}}) is None
    assert webhook.account_id_of({"Account": {"id": "abc"}}) is None
    assert webhook.account_id_of({}) is None


def test_own_account_matching():
    assert webhook.is_own_account(MINE, 1) is True
    assert webhook.is_own_account(THEIRS, 1) is False


def test_nothing_is_accepted_before_an_account_is_chosen():
    """Accepting everything on a shared server is the exact failure to avoid."""
    assert webhook.is_own_account(MINE, None) is False


def test_parse_extracts_the_fields_the_pipeline_needs():
    parsed = webhook.parse(MINE)
    assert parsed.media_kind == "episode"
    assert parsed.season == 2
    assert parsed.episode == 14
    assert parsed.rating_key == "48291"
    assert parsed.show_rating_key == "48270"
    assert parsed.section_id == 3
    assert parsed.show_title == "Re:ZERO -Starting Life in Another World-"


def test_the_payload_carries_no_external_ids():
    """Why a Plex server connection is mandatory rather than optional."""
    metadata = MINE["Metadata"]
    assert "grandparentGuid" not in metadata
    assert not metadata["guid"].startswith(("tvdb://", "tmdb://", "imdb://"))


# --------------------------------------------------------------------------
# the endpoint
# --------------------------------------------------------------------------
@pytest.fixture
def client(temp_db, settings):
    plex_client.save_account(PlexAccountInfo(1, "me", None))
    app = FastAPI()
    app.include_router(routes_webhook.router)
    with TestClient(app) as test_client:
        yield test_client


def url(path_token: str | None = None) -> str:
    return f"/webhook/plex/{path_token or auth.webhook_path_token()}"


def test_an_event_from_another_account_leaves_no_trace(client):
    response = client.post(url(), json=THEIRS)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"

    assert db.get_counter(routes_webhook.COUNTER_FOREIGN) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0

    # Nothing about their viewing may have been written anywhere.
    for table in ("event", "show", "write_log"):
        rows = db.query(f"SELECT * FROM {table}")
        blob = " ".join(str(dict(r)) for r in rows)
        assert FOREIGN_TITLE not in blob
        assert FOREIGN_EPISODE_TITLE not in blob


def test_many_foreign_events_only_move_a_counter(client):
    for _ in range(5):
        client.post(url(), json=THEIRS)
    assert db.get_counter(routes_webhook.COUNTER_FOREIGN) == 5
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0


def test_my_own_event_is_accepted(client):
    response = client.post(url(), json=MINE)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert db.get_counter(routes_webhook.COUNTER_ACCEPTED) == 1


def test_my_own_event_is_recorded_even_when_nothing_can_be_matched(client):
    """With no Plex connection there are no ids, so it should land in review."""
    client.post(url(), json=MINE)
    row = db.query_one("SELECT status, show_title FROM event ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert row["status"] == "unmatched"


def test_other_event_types_are_ignored(client):
    response = client.post(url(), json=PAUSE)
    assert response.json()["status"] == "ignored"
    assert db.get_counter(routes_webhook.COUNTER_IGNORED) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0


def test_a_wrong_path_token_is_a_404(client):
    response = client.post(url("deadbeef" * 4), json=MINE)
    assert response.status_code == 404
    assert db.get_counter(routes_webhook.COUNTER_ACCEPTED) == 0


def test_plex_posts_multipart_and_that_works(client):
    """Plex sends the JSON in a `payload` part, not as a JSON body."""
    response = client.post(url(), files={"payload": (None, json.dumps(MINE))})
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_an_unparseable_body_is_a_400(client):
    response = client.post(
        url(), content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400


def test_the_probe_endpoint_reports_the_configured_account(client):
    response = client.get(url())
    assert response.status_code == 200
    assert response.json()["configured_account_id"] == 1


def test_the_probe_endpoint_hides_behind_the_token(client):
    assert client.get(url("00000000" * 4)).status_code == 404
