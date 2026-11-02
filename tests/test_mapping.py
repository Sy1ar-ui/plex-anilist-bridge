"""The mapping engine: the cases PlexAniSync got wrong or made the user fix by hand."""
from __future__ import annotations

from app.mapping import engine
from app.models import LAYER_COMMUNITY, LAYER_FRIBB, LAYER_OVERRIDE
from tests.conftest import add_override, make_event, seed_media, seed_seasons

ONE_PIECE_GUID = "plex://show/5d9c084fec357c001f9ab4d8"
GINTAMA_GUID = "plex://show/5d9c08273c3f87001f348b54"
REZERO_TVDB = 305089


# --------------------------------------------------------------------------
# the plain case
# --------------------------------------------------------------------------
def test_single_cour_series(datasets):
    result = engine.resolve(make_event(tvdb_id=72025, season=1, episode=5))
    assert result.confidence == "confident"
    assert result.candidate.anilist_id == 290
    assert result.candidate.entry_episode == 5
    assert result.candidate.layer == LAYER_FRIBB


def test_unknown_show_is_unmatched_and_says_why(datasets):
    result = engine.resolve(make_event(tvdb_id=999999, season=1, episode=1))
    assert result.confidence == "unmatched"
    assert result.candidate is None
    assert "no entry for season 1" in " ".join(s.detail for s in result.steps)


def test_a_show_with_no_external_ids_explains_the_gap(datasets):
    result = engine.resolve(make_event(season=1, episode=1))
    assert result.confidence == "unmatched"
    assert "no TVDB or TMDB id" in " ".join(s.detail for s in result.steps)


# --------------------------------------------------------------------------
# split cours -- the case that needed hand-written YAML before
# --------------------------------------------------------------------------
def test_split_cour_first_half(datasets):
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=2, episode=13))
    assert result.candidate.anilist_id == 108632
    assert result.candidate.entry_episode == 13
    assert result.confidence == "confident"


def test_split_cour_second_half_restarts_at_episode_one(datasets):
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=2, episode=14))
    assert result.candidate.anilist_id == 119661
    assert result.candidate.entry_episode == 1
    assert result.confidence == "confident"


def test_the_entry_that_lost_a_split_is_explained(datasets):
    """"Why part 2 and not part 1?" has to be answerable from the trace."""
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=2, episode=14))
    losers = [s for s in result.steps if s.outcome in ("rejected", "skipped")]
    blob = " ".join(s.detail for s in losers)
    assert "108632" in blob
    assert "starts later and still covers it" in blob


def test_an_entry_that_starts_too_late_is_explained(datasets):
    """The mirror case: for episode 13, part 2 has not started yet."""
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=2, episode=13))
    rejected = [s for s in result.steps if s.outcome == "rejected"]
    blob = " ".join(s.detail for s in rejected)
    assert "119661" in blob
    assert "after episode 13" in blob


def test_tvdb_offsets_are_used_not_tmdb_offsets(datasets):
    """With the tmdb offset (26) this would resolve to a nonsensical episode."""
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=2, episode=20))
    assert result.candidate.anilist_id == 119661
    assert result.candidate.entry_episode == 7


# --------------------------------------------------------------------------
# absolute numbering -- One Piece
# --------------------------------------------------------------------------
def test_absolute_numbering_first_season_needs_no_arithmetic(datasets):
    seed_seasons(ONE_PIECE_GUID, {1: 61})
    result = engine.resolve(
        make_event(tvdb_id=81797, plex_guid=ONE_PIECE_GUID, season=1, episode=7)
    )
    assert result.candidate.anilist_id == 21
    assert result.candidate.entry_episode == 7
    assert result.confidence == "confident"


def test_absolute_numbering_adds_up_earlier_seasons(datasets):
    seed_seasons(ONE_PIECE_GUID, {1: 61, 2: 16, 3: 13})
    result = engine.resolve(
        make_event(tvdb_id=81797, plex_guid=ONE_PIECE_GUID, season=3, episode=5)
    )
    assert result.candidate.anilist_id == 21
    assert result.candidate.entry_episode == 61 + 16 + 5
    assert result.confidence == "confident"
    assert "absolute episode 82" in result.summary


def test_absolute_numbering_is_uncertain_when_a_season_has_gaps(datasets):
    """If the library is missing episodes the running total is wrong, so say so."""
    seed_seasons(ONE_PIECE_GUID, {1: 61, 2: 16}, gaps={2})
    result = engine.resolve(
        make_event(tvdb_id=81797, plex_guid=ONE_PIECE_GUID, season=3, episode=1)
    )
    assert result.confidence == "uncertain"
    assert "missing episodes" in result.summary


def test_absolute_numbering_is_uncertain_when_a_season_is_absent(datasets):
    seed_seasons(ONE_PIECE_GUID, {1: 61})
    result = engine.resolve(
        make_event(tvdb_id=81797, plex_guid=ONE_PIECE_GUID, season=3, episode=1)
    )
    assert result.confidence == "uncertain"
    assert "not in the Plex library" in result.summary


# --------------------------------------------------------------------------
# specials
# --------------------------------------------------------------------------
def test_specials_resolve_through_the_normal_path(datasets):
    """Season 0 is just another season here; skipping it is the pipeline's call."""
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=0, episode=1))
    assert result.candidate.anilist_id == 100049
    assert result.candidate.entry_episode == 1


def test_specials_honour_their_offset(datasets):
    """The One Piece season 0 entry has offset 1, so E01 is before it starts."""
    first = engine.resolve(make_event(tvdb_id=81797, season=0, episode=1))
    assert first.candidate is None

    second = engine.resolve(make_event(tvdb_id=81797, season=0, episode=2))
    assert second.candidate.anilist_id == 459
    assert second.candidate.entry_episode == 1


# --------------------------------------------------------------------------
# movies
# --------------------------------------------------------------------------
def test_movie_by_tmdb_id(datasets):
    result = engine.resolve(
        make_event(tmdb_id=128, media_kind="movie", season=None, episode=None)
    )
    assert result.confidence == "confident"
    assert result.candidate.anilist_id == 164
    assert result.candidate.entry_episode == 1


def test_movie_by_imdb_id(datasets):
    result = engine.resolve(
        make_event(imdb_id="tt0119698", media_kind="movie", season=None, episode=None)
    )
    assert result.candidate.anilist_id == 164


def test_movie_from_the_community_dataset(datasets):
    result = engine.resolve(
        make_event(
            plex_guid="plex://movie/bbbbbbbbbbbbbbbbbbbbbbbb",
            media_kind="movie",
            season=None,
            episode=None,
        )
    )
    assert result.candidate.anilist_id == 555002
    assert result.candidate.layer == LAYER_COMMUNITY


def test_unmatched_movie_explains_what_was_tried(datasets):
    result = engine.resolve(
        make_event(tmdb_id=424242, media_kind="movie", season=None, episode=None)
    )
    assert result.confidence == "unmatched"
    assert "tmdb 424242" in result.summary


# --------------------------------------------------------------------------
# community dataset
# --------------------------------------------------------------------------
def test_community_dataset_is_preferred_over_the_id_dataset(datasets):
    result = engine.resolve(
        make_event(tvdb_id=79895, plex_guid=GINTAMA_GUID, season=5, episode=3)
    )
    assert result.candidate.layer == LAYER_COMMUNITY
    assert result.candidate.anilist_id == 9969
    assert result.candidate.entry_episode == 3


def test_community_split_season(datasets):
    twelve = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=10, episode=12))
    assert (twelve.candidate.anilist_id, twelve.candidate.entry_episode) == (100784, 12)

    thirteen = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=10, episode=13))
    assert (thirteen.candidate.anilist_id, thirteen.candidate.entry_episode) == (101925, 1)


def test_several_plex_seasons_continuing_one_entry(datasets):
    """Gintama seasons 1-4 all map to AniList 918, so the count must carry over."""
    seed_seasons(GINTAMA_GUID, {1: 49, 2: 50, 3: 51})
    first = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=1, episode=1))
    assert first.candidate.entry_episode == 1

    second = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=2, episode=1))
    assert second.candidate.anilist_id == 918
    assert second.candidate.entry_episode == 50

    third = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=3, episode=2))
    assert third.candidate.entry_episode == 49 + 50 + 2


def test_continuation_without_season_counts_is_uncertain(datasets):
    result = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=2, episode=1))
    assert result.confidence == "uncertain"


def test_community_negative_offset_runs_one_ahead(datasets):
    result = engine.resolve(
        make_event(plex_guid="plex://show/aaaaaaaaaaaaaaaaaaaaaaaa", season=1, episode=5)
    )
    assert result.candidate.anilist_id == 555001
    assert result.candidate.entry_episode == 6


def test_community_miss_names_the_seasons_it_does_cover(datasets):
    result = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=99, episode=1))
    detail = " ".join(s.detail for s in result.steps if s.layer == LAYER_COMMUNITY)
    assert "but not season 99" in detail


# --------------------------------------------------------------------------
# overrides
# --------------------------------------------------------------------------
def test_override_beats_every_dataset(datasets):
    add_override(scope_key=GINTAMA_GUID, season=5, ep_from=1, anilist_id=4242)
    result = engine.resolve(
        make_event(tvdb_id=79895, plex_guid=GINTAMA_GUID, season=5, episode=3)
    )
    assert result.confidence == "override"
    assert result.candidate.anilist_id == 4242
    assert result.candidate.layer == LAYER_OVERRIDE


def test_override_range_applies_an_offset(datasets):
    add_override(scope_key=GINTAMA_GUID, season=2, ep_from=13, anilist_id=777, ep_offset=12)
    result = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=2, episode=14))
    assert result.candidate.anilist_id == 777
    assert result.candidate.entry_episode == 2


def test_override_outside_its_range_falls_through(datasets):
    add_override(
        scope_key=GINTAMA_GUID, season=5, ep_from=10, ep_to=20, anilist_id=4242
    )
    result = engine.resolve(
        make_event(tvdb_id=79895, plex_guid=GINTAMA_GUID, season=5, episode=3)
    )
    assert result.candidate.anilist_id == 9969


def test_override_can_be_scoped_to_a_tvdb_id(datasets):
    add_override(
        scope_type="tvdb", scope_key="72025", season=1, ep_from=1, anilist_id=5151
    )
    result = engine.resolve(make_event(tvdb_id=72025, season=1, episode=4))
    assert result.candidate.anilist_id == 5151


def test_the_narrowest_matching_override_wins(datasets):
    add_override(scope_key=GINTAMA_GUID, season=1, ep_from=1, anilist_id=111)
    add_override(scope_key=GINTAMA_GUID, season=1, ep_from=20, anilist_id=222, ep_offset=19)
    result = engine.resolve(make_event(plex_guid=GINTAMA_GUID, season=1, episode=25))
    assert result.candidate.anilist_id == 222
    assert result.candidate.entry_episode == 6


# --------------------------------------------------------------------------
# sanity checks against the cached episode counts
# --------------------------------------------------------------------------
def test_entry_episode_past_the_end_is_uncertain(datasets):
    """Usually means the dataset has not caught up with a newly aired cour."""
    seed_media(21355, episodes=25, title="Re:Zero")
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=1, episode=30))
    assert result.confidence == "uncertain"
    assert "past the 25 episodes" in result.summary


def test_entry_episode_within_the_count_stays_confident(datasets):
    seed_media(21355, episodes=25, title="Re:Zero")
    result = engine.resolve(make_event(tvdb_id=REZERO_TVDB, season=1, episode=25))
    assert result.confidence == "confident"
    assert result.candidate.entry_episode == 25


def test_titles_appear_in_the_trace_once_cached(datasets):
    seed_media(290, episodes=13, title="Crest of the Stars")
    result = engine.resolve(make_event(tvdb_id=72025, season=1, episode=1))
    assert "Crest of the Stars" in result.summary


def test_trace_serialises(datasets):
    result = engine.resolve(make_event(tvdb_id=72025, season=1, episode=1))
    import json

    payload = json.loads(result.trace_json())
    assert payload["confidence"] == "confident"
    assert payload["chosen"]["anilist_id"] == 290
    assert payload["steps"]
