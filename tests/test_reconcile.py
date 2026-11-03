"""The history reconcile reads every id the chosen account goes by."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.sync import reconcile

WHEN = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)


def play(rating_key: str, account_id: int, viewed: str = "2026-11-03 01:00:00"):
    return SimpleNamespace(ratingKey=rating_key, viewedAt=viewed, accountID=account_id)


class FakePlex:
    def __init__(self, by_account: dict[int, list], failing: set[int] = frozenset()):
        self.by_account = by_account
        self.failing = failing
        self.asked: list[int] = []

    def history_sync(self, *, account_id: int, mindate):
        self.asked.append(account_id)
        if account_id in self.failing:
            raise RuntimeError("Plex said no")
        return list(self.by_account.get(account_id, []))


def test_history_is_read_for_each_id_and_each_play_kept_once():
    plex = FakePlex({
        1: [play("100", 1), play("101", 1)],
        23834791: [play("101", 23834791), play("102", 23834791)],   # 101 is the same play
    })
    items = asyncio.run(reconcile.history_for(plex, [1, 23834791], WHEN))
    assert plex.asked == [1, 23834791]
    assert [i.ratingKey for i in items] == ["100", "101", "102"]


def test_a_failing_extra_id_does_not_stop_the_reconcile():
    plex = FakePlex({1: [play("100", 1)]}, failing={23834791})
    items = asyncio.run(reconcile.history_for(plex, [1, 23834791], WHEN))
    assert [i.ratingKey for i in items] == ["100"]


def test_the_chosen_accounts_own_history_must_be_readable():
    plex = FakePlex({}, failing={1})
    with pytest.raises(RuntimeError):
        asyncio.run(reconcile.history_for(plex, [1, 23834791], WHEN))


def test_the_reconcile_asks_for_the_owners_plex_tv_id_too(temp_db, settings, monkeypatch):
    from app.plex import client as plex_client

    plex_client.save_server("http://plex:32400", "token", name="tower", machine_id="aaaa")
    plex_client.save_account(plex_client.PlexAccountInfo(1, "Ja_es", None))
    plex_client.save_owner_plex_tv_id(23834791, "aaaa")
    asked = []

    async def history_for(plex, account_ids, mindate):
        asked.append(account_ids)
        return []

    monkeypatch.setattr(reconcile, "history_for", history_for)
    asyncio.run(reconcile.run_reconcile())
    assert asked == [[1, 23834791]]
