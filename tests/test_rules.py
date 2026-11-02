"""The AniList update rules.

These are the rules that decide whether anything is written at all, so they are
tested against the serialised variables rather than intent -- in particular the
assertion that score and notes are *absent*, not merely unchanged.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.anilist.rules import (
    STATUS_COMPLETED,
    STATUS_CURRENT,
    STATUS_REPEATING,
    RuleSettings,
    plan_anilist_write,
)
from app.models import AniListEntryState

TODAY = date(2026, 11, 2)


def state(**kw) -> AniListEntryState:
    defaults = {
        "anilist_id": 21355,
        "exists": True,
        "status": STATUS_CURRENT,
        "progress": 0,
        "repeat": 0,
        "total_episodes": 25,
        "media_status": "FINISHED",
        "title": "Re:Zero",
    }
    defaults.update(kw)
    return AniListEntryState(**defaults)


def rules(**kw) -> RuleSettings:
    defaults = {"add_untracked": True, "rewatch_action": "ignore", "set_dates": True, "today": TODAY}
    defaults.update(kw)
    return RuleSettings(**defaults)


# --------------------------------------------------------------------------
# never decrease
# --------------------------------------------------------------------------
@pytest.mark.parametrize("watched", [1, 5, 11, 12])
def test_progress_is_never_lowered(watched):
    plan = plan_anilist_write(state(progress=12), watched, rules())
    assert plan.action == "no_change"
    assert plan.diff is None
    assert "never lowered" in plan.reason


def test_progress_increase_is_written():
    plan = plan_anilist_write(state(progress=12), 13, rules())
    assert plan.action == "write"
    assert plan.diff.variables["progress"] == 13


def test_writes_are_absolute_not_increments():
    """This is what makes a replayed event harmless."""
    plan = plan_anilist_write(state(progress=4), 9, rules())
    assert plan.diff.variables["progress"] == 9


def test_an_impossible_entry_episode_is_skipped():
    plan = plan_anilist_write(state(), 0, rules())
    assert plan.action == "skipped"


# --------------------------------------------------------------------------
# untracked titles
# --------------------------------------------------------------------------
def test_untracked_title_is_added_as_current():
    plan = plan_anilist_write(state(exists=False, status=None), 1, rules())
    assert plan.action == "write"
    assert plan.diff.variables["status"] == STATUS_CURRENT
    assert plan.diff.variables["progress"] == 1
    assert plan.diff.variables["startedAt"] == {"year": 2026, "month": 11, "day": 2}


def test_untracked_title_is_left_alone_when_that_is_turned_off():
    plan = plan_anilist_write(
        state(exists=False, status=None), 1, rules(add_untracked=False)
    )
    assert plan.action == "skipped"
    assert "not on your AniList list" in plan.reason


def test_untracked_binge_to_the_end_is_added_as_completed():
    plan = plan_anilist_write(state(exists=False, status=None), 25, rules())
    assert plan.diff.variables["status"] == STATUS_COMPLETED
    assert "completedAt" in plan.diff.variables


# --------------------------------------------------------------------------
# status transitions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("before", ["PLANNING", "PAUSED", "DROPPED", None])
def test_starting_a_planned_or_abandoned_title_sets_current(before):
    plan = plan_anilist_write(state(status=before, progress=0), 1, rules())
    assert plan.diff.variables["status"] == STATUS_CURRENT


def test_final_episode_completes_a_finished_series():
    plan = plan_anilist_write(state(progress=24), 25, rules())
    assert plan.diff.variables["status"] == STATUS_COMPLETED
    assert plan.diff.variables["completedAt"] == {"year": 2026, "month": 11, "day": 2}


def test_a_still_airing_series_is_not_completed_early():
    """AniList's episode count for a releasing show is the announced total."""
    plan = plan_anilist_write(
        state(progress=11, total_episodes=12, media_status="RELEASING"), 12, rules()
    )
    assert plan.diff.variables["status"] == STATUS_CURRENT
    assert "completedAt" not in plan.diff.variables
    assert "not marked finished" in plan.reason


def test_a_series_with_no_episode_count_is_not_completed():
    plan = plan_anilist_write(state(progress=4, total_episodes=None), 5, rules())
    assert plan.diff.variables["status"] == STATUS_CURRENT


def test_dates_can_be_turned_off():
    plan = plan_anilist_write(state(progress=24), 25, rules(set_dates=False))
    assert "completedAt" not in plan.diff.variables
    assert "startedAt" not in plan.diff.variables


def test_a_started_date_is_not_overwritten():
    plan = plan_anilist_write(
        state(status="PLANNING", progress=0, started_at={"year": 2020, "month": 1, "day": 1}),
        1,
        rules(),
    )
    assert "startedAt" not in plan.diff.variables


# --------------------------------------------------------------------------
# rewatching
# --------------------------------------------------------------------------
def test_rewatching_a_completed_title_is_ignored_by_default():
    plan = plan_anilist_write(
        state(status=STATUS_COMPLETED, progress=25), 1, rules()
    )
    assert plan.action == "no_change"
    assert "rewatch syncing is set to ignore" in plan.reason


def test_rewatching_can_set_repeating():
    plan = plan_anilist_write(
        state(status=STATUS_COMPLETED, progress=25), 1, rules(rewatch_action="repeating")
    )
    assert plan.diff.variables["status"] == STATUS_REPEATING
    assert plan.diff.variables["progress"] == 1


def test_a_rewatch_in_progress_advances():
    plan = plan_anilist_write(
        state(status=STATUS_REPEATING, progress=3), 4, rules(rewatch_action="repeating")
    )
    assert plan.diff.variables["status"] == STATUS_REPEATING
    assert plan.diff.variables["progress"] == 4


def test_a_rewatch_does_not_go_backwards_either():
    plan = plan_anilist_write(
        state(status=STATUS_REPEATING, progress=8), 3, rules(rewatch_action="repeating")
    )
    assert plan.action == "no_change"


def test_finishing_a_rewatch_completes_and_counts_it():
    plan = plan_anilist_write(
        state(status=STATUS_REPEATING, progress=24, repeat=1),
        25,
        rules(rewatch_action="repeating"),
    )
    assert plan.diff.variables["status"] == STATUS_COMPLETED
    assert plan.diff.variables["repeat"] == 2


# --------------------------------------------------------------------------
# what must never be written
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "case",
    [
        {"exists": False, "status": None},
        {"status": "PLANNING", "progress": 0},
        {"status": STATUS_CURRENT, "progress": 10},
        {"status": STATUS_REPEATING, "progress": 2},
        {"status": STATUS_COMPLETED, "progress": 25},
    ],
)
def test_scores_and_notes_are_never_in_the_payload(case):
    plan = plan_anilist_write(state(**case), 11, rules(rewatch_action="repeating"))
    if plan.diff is None:
        return
    forbidden = {"score", "scoreRaw", "notes", "advancedScores", "private"}
    assert not forbidden & set(plan.diff.variables)


def test_the_diff_describes_before_and_after():
    plan = plan_anilist_write(state(progress=12), 13, rules())
    assert plan.diff.before["progress"] == 12
    assert plan.diff.after["progress"] == 13
    assert plan.diff.before["status"] == STATUS_CURRENT
