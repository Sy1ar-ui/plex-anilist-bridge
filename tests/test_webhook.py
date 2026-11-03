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


def _last_webhook():
    return db.query_one("SELECT value FROM watermark WHERE name = 'last_webhook'")


def test_any_event_from_my_account_proves_the_webhook_works(client):
    """Plex scrobbles only at ~90% watched; a quick play/stop must still count."""
    assert _last_webhook() is None
    client.post(url(), json=PAUSE)
    assert _last_webhook() is not None
    # ...but only a scrobble is synced.
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0


def test_another_accounts_events_never_mark_the_webhook_as_working(client):
    client.post(url(), json=THEIRS)
    client.post(url(), json={**THEIRS, "event": "media.pause"})
    assert _last_webhook() is None


def test_another_accounts_other_event_types_only_move_the_foreign_counter(client):
    response = client.post(url(), json={**THEIRS, "event": "media.play"})
    assert response.json() == {"status": "ignored", "reason": "other account"}
    assert db.get_counter(routes_webhook.COUNTER_FOREIGN) == 1
    assert db.get_counter(routes_webhook.COUNTER_IGNORED) == 0


def _connect_server(machine_id: str = "aaaa-bbbb-cccc") -> None:
    plex_client.save_server("http://plex:32400", "token", name="tower", machine_id=machine_id)


def test_the_server_id_is_read_from_the_payload():
    assert webhook.server_uuid_of(MINE) == "aaaa-bbbb-cccc"
    assert webhook.server_uuid_of(PAUSE) is None
    assert webhook.is_from_server(MINE, "aaaa-bbbb-cccc")
    assert not webhook.is_from_server(MINE, "zzzz")
    # Unknown on either side: nothing to compare, so it is not refused.
    assert webhook.is_from_server(MINE, None)
    assert webhook.is_from_server(PAUSE, "zzzz")


def test_my_event_from_another_server_is_dropped(client):
    """Plex sends your own playback on servers you are shared on; its rating keys are not ours."""
    _connect_server("a-different-server")
    response = client.post(url(), json=MINE)
    assert response.json() == {"status": "ignored", "reason": "other server"}
    assert db.get_counter(routes_webhook.COUNTER_OTHER_SERVER) == 1
    assert db.get_counter(routes_webhook.COUNTER_ACCEPTED) == 0
    assert _last_webhook() is None
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0


def test_my_event_from_the_connected_server_is_accepted(client):
    _connect_server("aaaa-bbbb-cccc")
    assert client.post(url(), json=MINE).json()["status"] == "accepted"
    assert db.get_counter(routes_webhook.COUNTER_OTHER_SERVER) == 0


def test_another_server_never_reaches_the_account_filter(client):
    _connect_server("a-different-server")
    client.post(url(), json=THEIRS)
    assert db.get_counter(routes_webhook.COUNTER_OTHER_SERVER) == 1
    assert db.get_counter(routes_webhook.COUNTER_FOREIGN) == 0


# --------------------------------------------------------------------------
# my own events under another id: noticed, offered, accepted only once confirmed
# --------------------------------------------------------------------------
def _mine_as(account_id, *, event="media.play", **extra):
    return {**MINE, "event": event, "Account": {**MINE["Account"], "id": account_id}, **extra}


def test_my_event_under_another_id_is_dropped_but_its_id_is_offered(client):
    """Named like the chosen account ("me"), so the id is kept as a candidate."""
    response = client.post(url(), json=_mine_as(4242, owner=False, user=False))
    assert response.json()["reason"] == "other account"
    assert plex_client.webhook_account_candidate() == {"id": 4242, "seen": 1, "reasons": ["name"]}
    assert _last_webhook() is None
    assert db.query_one("SELECT COUNT(*) AS n FROM event")["n"] == 0


def test_plexs_owner_flags_also_identify_the_owner(client):
    payload = _mine_as(4242, user=True, owner=True)
    payload["Account"] = {**payload["Account"], "title": "James"}
    client.post(url(), json=payload)
    client.post(url(), json=payload)
    assert plex_client.webhook_account_candidate() == {"id": 4242, "seen": 2, "reasons": ["owner"]}


def test_owner_flags_count_only_when_the_owner_is_chosen(client):
    plex_client.save_account(PlexAccountInfo(812, "lodger", None))
    payload = _mine_as(4242, user=True, owner=True)
    payload["Account"] = {**payload["Account"], "title": "James"}
    client.post(url(), json=payload)
    assert plex_client.webhook_account_candidate() is None


def test_another_persons_event_is_never_offered(client):
    client.post(url(), json=THEIRS)
    client.post(url(), json={**THEIRS, "event": "media.stop"})
    assert plex_client.webhook_account_candidate() is None


def test_a_confirmed_id_counts_as_mine(client):
    plex_client.set_webhook_account_alias(4242)
    assert client.post(url(), json=_mine_as(4242, event="media.scrobble")).json()["status"] == "accepted"
    assert _last_webhook() is not None
    # The chosen account's own id still works too.
    assert client.post(url(), json=MINE).json()["status"] == "accepted"


def test_choosing_an_account_forgets_a_confirmed_id(client):
    plex_client.set_webhook_account_alias(4242)
    plex_client.note_webhook_account_candidate(5151, ["name"])
    plex_client.save_account(PlexAccountInfo(1, "me", None))
    assert plex_client.webhook_account_alias() is None
    assert plex_client.webhook_account_candidate() is None


def _signed_in_as_owner(plex_tv_id=23834791, owned=True):
    from app import settings_store

    settings_store.set(plex_client.SIGNED_IN_AS,
                       {"id": plex_tv_id, "username": "Ja_es", "title": "James", "owned": owned})


def test_the_owners_plex_tv_id_counts_without_asking(client):
    """What a real server sent: the owner's events under their plex.tv id, not 1."""
    _signed_in_as_owner()
    response = client.post(url(), json=_mine_as(23834791, event="media.scrobble"))
    assert response.json()["status"] == "accepted"
    assert plex_client.webhook_account_candidate() is None
    # A play/stop marks the webhook as working, too.
    db.execute("DELETE FROM watermark WHERE name = 'last_webhook'")
    client.post(url(), json=_mine_as(23834791, event="media.stop"))
    assert _last_webhook() is not None


def test_the_plex_tv_id_counts_only_for_the_chosen_owner(client):
    _signed_in_as_owner()
    plex_client.save_account(PlexAccountInfo(812, "lodger", None))
    assert plex_client.owner_plex_tv_id() is None
    assert client.post(url(), json=_mine_as(23834791)).json()["reason"] == "other account"


def test_the_plex_tv_id_counts_only_when_the_sign_in_owns_the_server(client):
    _signed_in_as_owner(owned=False)
    assert plex_client.owner_plex_tv_id() is None
    assert client.post(url(), json=_mine_as(23834791)).json()["reason"] == "other account"


def test_an_older_sign_in_without_an_id_still_asks(client):
    """Installs set up before the id was recorded fall back to the prompt."""
    _signed_in_as_owner(plex_tv_id=None)
    assert plex_client.owner_plex_tv_id() is None
    client.post(url(), json=_mine_as(23834791))
    assert plex_client.webhook_account_candidate()["id"] == 23834791


def test_own_names_include_the_plex_sign_in(client):
    from app import settings_store

    settings_store.set(plex_client.SIGNED_IN_AS, {"username": "Ja_es", "title": "James Alcock", "owned": True})
    assert plex_client.own_names() == {"me", "ja_es", "james alcock"}
    plex_client.save_account(PlexAccountInfo(1, "1", None))   # saved with no name: the id stands in
    assert plex_client.own_names() == {"ja_es", "james alcock"}


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
