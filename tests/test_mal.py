"""The AniList -> MyAnimeList leg: rules, client, poller, full reconcile, page."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, oauth, settings_store, tokens
from app.main import create_app
from app.mal import reconcile as mal_reconcile
from app.mal import sync as mal_sync
from app.mal.client import MALAuthError, MALClient, MALError, MALUnavailable, normalise
from app.mal.rules import MalSettings, desired, mal_score, plan_mal_write
from app.sync.writegate import WriteGate


def entry(anilist_id=1, mal_id=101, *, status="CURRENT", progress=3, score=0, repeat=0, updated=1000,
          started=None, completed=None, title="Show"):
    return {
        "id": anilist_id * 10, "status": status, "progress": progress, "repeat": repeat,
        "updatedAt": updated, "score": score, "startedAt": started, "completedAt": completed,
        "media": {"id": anilist_id, "idMal": mal_id, "episodes": 12,
                  "title": {"english": title, "romaji": title}},
    }


def mal_state(status="watching", episodes=3, score=0, rewatching=False, times=0):
    return {"status": status, "is_rewatching": rewatching, "num_watched_episodes": episodes,
            "score": score, "num_times_rewatched": times, "start_date": None, "finish_date": None}


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
@pytest.mark.parametrize("anilist,mal,rewatching", [
    ("CURRENT", "watching", False), ("PLANNING", "plan_to_watch", False),
    ("COMPLETED", "completed", False), ("DROPPED", "dropped", False),
    ("PAUSED", "on_hold", False), ("REPEATING", "completed", True),
])
def test_statuses_map_across(anilist, mal, rewatching):
    want = desired(entry(status=anilist), MalSettings())
    assert (want["status"], want["is_rewatching"]) == (mal, rewatching)


def test_a_title_not_on_mal_is_added_whole():
    plan = plan_mal_write(entry(progress=5, score=80), None)
    assert plan.action == "write"
    assert plan.fields == {"status": "watching", "is_rewatching": False, "num_watched_episodes": 5, "score": 8}


def test_only_what_differs_is_sent():
    plan = plan_mal_write(entry(progress=7), mal_state(episodes=3))
    assert plan.fields == {"num_watched_episodes": 7}


def test_matching_entries_are_left_alone():
    assert plan_mal_write(entry(progress=3), mal_state(episodes=3)).action == "no_change"


def test_progress_is_never_lowered_automatically():
    plan = plan_mal_write(entry(progress=2), mal_state(episodes=9))
    assert plan.action == "skipped"
    assert "never lowered" in plan.reason
    assert plan.lowers == ["episodes 9 -> 2"]


def test_lowering_is_allowed_only_when_approved():
    plan = plan_mal_write(entry(progress=2), mal_state(episodes=9), allow_lower=True)
    assert plan.fields == {"num_watched_episodes": 2}


def test_a_completed_mal_entry_is_not_moved_back_automatically():
    plan = plan_mal_write(entry(status="CURRENT", progress=12), mal_state(status="completed", episodes=12))
    assert plan.action == "skipped"
    assert "status completed -> watching" in plan.lowers


def test_other_fields_still_go_when_a_lowering_is_held_back():
    plan = plan_mal_write(entry(progress=2, score=90), mal_state(episodes=9, score=5))
    assert plan.action == "write"
    assert plan.fields == {"score": 9}
    assert "held back" in plan.reason


def test_a_rewatch_restarting_the_episode_count_is_not_a_lowering():
    plan = plan_mal_write(entry(status="REPEATING", progress=2, repeat=1),
                          mal_state(status="completed", episodes=12))
    assert plan.action == "write"
    assert plan.fields == {"is_rewatching": True, "num_watched_episodes": 2, "num_times_rewatched": 1}
    assert plan.lowers == []


@pytest.mark.parametrize("anilist,expected", [(0, None), (None, None), (5, 1), (80, 8), (85, 9), (100, 10)])
def test_scores_convert_half_up_and_zero_means_unscored(anilist, expected):
    assert mal_score(anilist) == expected


def test_an_unscored_anilist_entry_never_clears_a_mal_score():
    assert plan_mal_write(entry(score=0), mal_state(score=7)).action == "no_change"


def test_scores_can_be_left_out():
    plan = plan_mal_write(entry(score=90), mal_state(), MalSettings(sync_score=False))
    assert plan.action == "no_change"


def test_only_complete_dates_are_sent_and_only_when_asked():
    e = entry(started={"year": 2026, "month": 10, "day": 3}, completed={"year": 2026, "month": 11})
    assert "start_date" not in desired(e, MalSettings())
    want = desired(e, MalSettings(sync_dates=True))
    assert want["start_date"] == "2026-10-03"
    assert "finish_date" not in want


def test_a_title_without_a_mal_id_is_skipped():
    plan = plan_mal_write(entry(mal_id=None), None)
    assert plan.action == "skipped"


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class MalServer:
    """Answers like api.myanimelist.net for the client tests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reject_token: str | None = None
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith(oauth.MAL_TOKEN):
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 2678400})
        if self.reject_token and request.headers["Authorization"] == f"Bearer {self.reject_token}":
            return httpx.Response(401, json={"error": "invalid_token"})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        if request.url.path == "/v2/anime/101":
            return httpx.Response(200, json={"id": 101, "my_list_status": {
                "status": "watching", "num_episodes_watched": 4, "score": 0, "is_rewatching": False}})
        if request.url.path == "/v2/users/@me/animelist":
            if "offset" in request.url.params:
                return httpx.Response(200, json={"data": [{"node": {"id": 2, "title": "B"},
                                                           "list_status": {"status": "completed"}}], "paging": {}})
            return httpx.Response(200, json={
                "data": [{"node": {"id": 1, "title": "A"}, "list_status": {"status": "watching", "num_episodes_watched": 2}}],
                "paging": {"next": "https://api.myanimelist.net/v2/users/@me/animelist?offset=1"}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"status": "watching"})
        return httpx.Response(404, json={})


def mal_client(server: MalServer, token="tok", **kw) -> MALClient:
    async def no_sleep(_s):
        return None
    kw.setdefault("sleep", no_sleep)
    return MALClient(token, http=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw)


def test_the_list_status_is_normalised_to_write_names():
    assert asyncio.run(mal_client(MalServer()).list_status(101))["num_watched_episodes"] == 4
    assert normalise(None) is None


def test_the_whole_list_follows_paging():
    full = asyncio.run(mal_client(MalServer()).animelist())
    assert sorted(full) == [1, 2]
    assert full[1]["title"] == "A"


def test_updates_send_only_writable_fields_as_form_values():
    server = MalServer()
    asyncio.run(mal_client(server).update(101, {"num_watched_episodes": 5, "is_rewatching": False}))
    form = parse_qs(server.requests[-1].content.decode())
    assert form == {"num_watched_episodes": ["5"], "is_rewatching": ["false"]}
    with pytest.raises(MALError, match="comments"):
        asyncio.run(mal_client(server).update(101, {"comments": "hello"}))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_throttling_and_outages_are_unavailable(status):
    server = MalServer()
    server.status = status
    with pytest.raises(MALUnavailable):
        asyncio.run(mal_client(server).list_status(101))


def test_requests_are_spaced_out():
    waits: list[float] = []

    async def record(seconds):
        waits.append(seconds)

    client = mal_client(MalServer(), min_interval=5.0, sleep=record)
    asyncio.run(client.list_status(101))
    asyncio.run(client.list_status(101))
    assert waits and waits[0] > 4


def test_a_401_refreshes_once_and_retries(temp_db):
    server = MalServer()
    server.reject_token = "old"

    async def refresh():
        return "fresh"

    assert asyncio.run(mal_client(server, token="old", refresh=refresh).list_status(101)) is not None
    assert server.requests[-1].headers["Authorization"] == "Bearer fresh"


def test_a_token_that_stays_rejected_is_marked_broken(temp_db):
    server = MalServer()
    server.reject_token = "old"

    async def refresh():
        return None

    with pytest.raises(MALAuthError):
        asyncio.run(mal_client(server, token="old", refresh=refresh).list_status(101))
    assert tokens.get(tokens.MAL).status == tokens.STATUS_BROKEN


def test_refresh_stores_the_new_tokens(temp_db, monkeypatch):
    from app.mal import client as mal_client_module

    server = MalServer()
    oauth.save_client(tokens.MAL, "malid", "")
    tokens.save(tokens.MAL, access_token="old", refresh_token="r1",
                expires_at=datetime.now(UTC) + timedelta(hours=1))
    new = asyncio.run(mal_client_module.refresh_stored_token(
        http=httpx.AsyncClient(transport=httpx.MockTransport(server))))
    assert new == "fresh"
    record = tokens.get(tokens.MAL)
    assert (record.access_token, record.refresh_token) == ("fresh", "r2")
    form = parse_qs(server.requests[-1].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["r1"]


# --------------------------------------------------------------------------
# the poller
# --------------------------------------------------------------------------
class FakeAniListList:
    """recent_list_changes newest first, and the whole list."""

    def __init__(self, entries):
        self.entries = entries
        self.pages_read = 0

    async def recent_list_changes(self, user_id, *, page=1, per_page=50):
        self.pages_read += 1
        ordered = sorted(self.entries, key=lambda e: -e["updatedAt"])
        chunk = ordered[(page - 1) * per_page: page * per_page]
        return chunk, page * per_page < len(ordered)

    async def list_collection(self, user_id):
        return list(self.entries)

    async def __aexit__(self, *exc):
        return None


class FakeMAL:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.updates: list[tuple[int, dict]] = []
        self.down_after: int | None = None   # go down after this many updates

    async def list_status(self, mal_id):
        return dict(self.state[mal_id]) if mal_id in self.state else None

    async def animelist(self):
        return {k: {**v, "title": f"MAL {k}"} for k, v in self.state.items()}

    async def update(self, mal_id, fields):
        if self.down_after is not None and len(self.updates) >= self.down_after:
            raise MALUnavailable("MyAnimeList returned HTTP 503")
        self.updates.append((mal_id, dict(fields)))
        self.state[mal_id] = {**self.state.get(mal_id, mal_state(episodes=0)), **fields}
        return {}

    async def __aexit__(self, *exc):
        return None


@pytest.fixture
def mirror(temp_db, settings):
    settings_store.set("mal_sync_enabled", True)
    tokens.save(tokens.ANILIST, access_token="a", account_name="me", account_id=7)
    tokens.save(tokens.MAL, access_token="m", account_name="me_mal")


def poll(anilist, mal, *, dry_run=False):
    return asyncio.run(mal_sync.poll(anilist=anilist, mal=mal, gate=WriteGate(dry_run=dry_run)))


def test_the_poller_does_nothing_while_turned_off(temp_db, settings):
    report = asyncio.run(mal_sync.poll(anilist=FakeAniListList([]), mal=FakeMAL()))
    assert "turned off" in report.detail


def test_the_first_run_only_starts_the_clock(mirror):
    mal = FakeMAL()
    report = poll(FakeAniListList([entry(updated=10**12)]), mal)
    assert "full reconcile" in report.detail
    assert mal.updates == []
    assert mal_sync.get_watermark(False) is not None


def test_changes_after_the_watermark_are_mirrored_oldest_first(mirror):
    mal_sync.set_watermark(False, 1000)
    changes = [entry(1, 101, progress=4, updated=1003), entry(2, 102, progress=1, updated=1001),
               entry(3, 103, updated=900)]   # before the watermark: ignored
    mal = FakeMAL({101: mal_state(episodes=3)})
    report = poll(FakeAniListList(changes), mal)
    assert [m for m, _ in mal.updates] == [102, 101]
    assert mal.updates[1][1] == {"num_watched_episodes": 4}
    assert report.outcomes == {"written": 2}
    assert mal_sync.get_watermark(False) == 1003
    # Nothing new: nothing written.
    assert poll(FakeAniListList(changes), mal).seen == 0


def test_dry_run_records_but_sends_nothing_and_keeps_its_own_clock(mirror):
    mal_sync.set_watermark(True, 1000)
    mal_sync.set_watermark(False, 500)
    mal = FakeMAL()
    poll(FakeAniListList([entry(updated=1002)]), mal, dry_run=True)
    assert mal.updates == []
    row = db.query_one("SELECT dry_run, target FROM write_log")
    assert (row["target"], row["dry_run"]) == ("mal", 1)
    assert mal_sync.get_watermark(True) == 1002
    assert mal_sync.get_watermark(False) == 500   # the real run still sees it later


def test_an_outage_stops_the_run_and_the_next_one_resumes(mirror):
    mal_sync.set_watermark(False, 1000)
    changes = [entry(1, 101, updated=1001), entry(2, 102, updated=1002), entry(3, 103, updated=1003)]
    mal = FakeMAL()
    mal.down_after = 1
    report = poll(FakeAniListList(changes), mal)
    assert "stopped early" in report.detail
    assert mal_sync.get_watermark(False) == 1001
    mal.down_after = None
    poll(FakeAniListList(changes), mal)
    assert [m for m, _ in mal.updates] == [101, 102, 103]


def test_unmapped_and_held_back_entries_are_logged(mirror):
    mal_sync.set_watermark(False, 1000)
    changes = [entry(1, None, updated=1001, title="No Id"), entry(2, 102, progress=1, updated=1002)]
    mal = FakeMAL({102: mal_state(episodes=9)})
    poll(FakeAniListList(changes), mal)
    log = {r["title"]: r for r in mal_sync.recent_log()}
    assert log["No Id"]["outcome"] == "skipped"
    assert "never lowered" in log["Show"]["detail"]
    assert mal.updates == []


# --------------------------------------------------------------------------
# full reconcile
# --------------------------------------------------------------------------
def build_plan(entries, mal_state_by_id):
    mal = FakeMAL(mal_state_by_id)
    plan_id = asyncio.run(mal_reconcile.build(anilist=FakeAniListList(entries), mal=mal))
    return plan_id, mal


def test_the_preview_sorts_every_difference(mirror):
    entries = [entry(1, 101, title="New"), entry(2, 102, progress=5, title="Behind"),
               entry(3, 103, progress=1, title="Ahead"), entry(4, 104, progress=3, title="Same"),
               entry(5, None, title="No Id")]
    plan_id, mal = build_plan(entries, {102: mal_state(episodes=2), 103: mal_state(episodes=8),
                                        104: mal_state(episodes=3), 999: mal_state()})
    groups = mal_reconcile.items(plan_id)
    assert [i["title"] for i in groups["add"]] == ["New"]
    assert [i["title"] for i in groups["update"]] == ["Behind"]
    assert [i["title"] for i in groups["lower"]] == ["Ahead"]
    assert [i["title"] for i in groups["unmapped"]] == ["No Id"]
    assert [i["mal_id"] for i in groups["only_on_mal"]] == [999]
    assert groups["lower"][0]["decision"] == "pending"       # not ticked by default
    assert groups["update"][0]["decision"] == "approved"
    assert mal_reconcile.draft()["summary"]["same"] == 1
    assert mal.updates == []                                 # a preview writes nothing


def test_applying_writes_only_what_was_ticked(mirror):
    plan_id, mal = build_plan([entry(1, 101), entry(3, 103, progress=1)], {103: mal_state(episodes=8)})
    groups = mal_reconcile.items(plan_id)
    add = groups["add"][0]
    assert groups["lower"]          # present, but left unticked below
    report = asyncio.run(mal_reconcile.apply(plan_id, {add["id"]}, mal=mal, gate=WriteGate(dry_run=False)))
    assert [m for m, _ in mal.updates] == [101]
    assert report.outcomes == {"applied": 1}
    assert mal.state[103]["num_watched_episodes"] == 8        # the unticked lowering was not made
    assert mal_reconcile.draft() is None                      # the plan is done


def test_an_approved_lowering_is_made(mirror):
    plan_id, mal = build_plan([entry(3, 103, progress=1)], {103: mal_state(episodes=8)})
    lower = mal_reconcile.items(plan_id)["lower"][0]
    asyncio.run(mal_reconcile.apply(plan_id, {lower["id"]}, mal=mal, gate=WriteGate(dry_run=False)))
    assert mal.state[103]["num_watched_episodes"] == 1


def test_applying_in_dry_run_sends_nothing(mirror):
    plan_id, mal = build_plan([entry(1, 101)], {})
    add = mal_reconcile.items(plan_id)["add"][0]
    report = asyncio.run(mal_reconcile.apply(plan_id, {add["id"]}, mal=mal, gate=WriteGate(dry_run=True)))
    assert mal.updates == []
    assert report.outcomes == {"would_write": 1}


def test_an_outage_while_applying_keeps_the_rest_for_later(mirror):
    plan_id, mal = build_plan([entry(1, 101, title="A"), entry(2, 102, title="B")], {})
    ids = {i["id"] for i in mal_reconcile.items(plan_id)["add"]}
    mal.down_after = 1
    report = asyncio.run(mal_reconcile.apply(plan_id, ids, mal=mal, gate=WriteGate(dry_run=False)))
    assert report.stopped
    assert mal_reconcile.draft()["id"] == plan_id
    mal.down_after = None
    asyncio.run(mal_reconcile.apply(plan_id, ids, mal=mal, gate=WriteGate(dry_run=False)))
    assert sorted(m for m, _ in mal.updates) == [101, 102]


def test_a_new_preview_replaces_the_old_one(mirror):
    first, _ = build_plan([entry(1, 101)], {})
    second, _ = build_plan([entry(1, 101)], {})
    assert mal_reconcile.draft()["id"] == second != first


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------
@pytest.fixture
def ui(datasets, settings):
    settings_store.set("setup_complete", True)
    return TestClient(create_app())


def test_the_page_says_why_the_mirror_is_off(ui):
    page = ui.get("/mal")
    assert page.status_code == 200
    assert "turned off in settings" in page.text
    assert 'href="/settings#mal"' in page.text


def test_the_settings_turn_it_on(ui):
    ui.post("/settings/behaviour", data={"dry_run": "on", "mal_sync_enabled": "on", "mal_sync_score": "on",
                                         "mal_poll_interval_minutes": "2"}, follow_redirects=False)
    assert settings_store.get("mal_sync_enabled") is True
    assert settings_store.get("mal_poll_interval_minutes") == 5    # clamped to the minimum


def test_preview_and_apply_through_the_page(ui, monkeypatch):
    from app.web import routes_mal

    settings_store.set("mal_sync_enabled", True)
    tokens.save(tokens.ANILIST, access_token="a", account_name="me", account_id=7)
    tokens.save(tokens.MAL, access_token="m", account_name="me_mal")
    anilist = FakeAniListList([entry(1, 101, title="New Show"), entry(3, 103, progress=1, title="Ahead")])
    mal = FakeMAL({103: mal_state(episodes=8)})

    class AniListFactory:
        @staticmethod
        def from_db(**_kw):
            return anilist

    class MALFactory:
        @staticmethod
        async def from_db(**_kw):
            return mal

    monkeypatch.setattr(routes_mal, "AniListClient", AniListFactory)
    monkeypatch.setattr(routes_mal, "MALClient", MALFactory)

    built = ui.post("/mal/reconcile", follow_redirects=False)
    assert "Preview+ready" in built.headers["location"]
    page = ui.get("/mal").text
    assert "Add to MyAnimeList" in page and "Would lower MyAnimeList" in page
    assert "New Show" in page

    plan = mal_reconcile.draft()
    add = mal_reconcile.items(plan["id"])["add"][0]
    applied = ui.post(f"/mal/reconcile/{plan['id']}/apply", data={"item": [str(add["id"])]},
                      follow_redirects=False)
    assert "level=ok" in applied.headers["location"]
    assert mal.updates == []            # dry run is on in the settings fixture
    assert "would write" in ui.get("/mal").text


def test_the_system_page_links_to_the_mirror(ui):
    assert 'href="/mal"' in ui.get("/system").text
