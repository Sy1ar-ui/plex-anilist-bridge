"""Dataset loading: the shapes that the mapping engine depends on."""
from __future__ import annotations

import pytest

from app import db
from app.datasets import community, fribb
from tests.conftest import FIXTURES


def test_fribb_slice_loads(datasets):
    assert fribb.row_count() == 10


def test_absolute_numbering_is_signalled_by_a_missing_season(datasets):
    """The whole absolute-numbering strategy rests on this being NULL."""
    row = db.query_one(
        "SELECT anilist_id, tvdb_season, tvdb_offset FROM ds_fribb WHERE anidb_id = 69"
    )
    assert row["anilist_id"] == 21
    assert row["tvdb_season"] is None
    assert row["tvdb_offset"] == 0


def test_tvdb_and_tmdb_offsets_are_kept_apart(datasets):
    """Re:Zero S2 part 1 has a tmdb offset of 26 but no tvdb offset.

    Collapsing these into one column would put TVDB-numbered libraries 26
    episodes out.
    """
    row = db.query_one(
        "SELECT tvdb_offset, tmdb_offset, tvdb_season, tmdb_season FROM ds_fribb"
        " WHERE anidb_id = 14792"
    )
    assert row["tvdb_offset"] == 0
    assert row["tmdb_offset"] == 26
    assert row["tvdb_season"] == 2
    assert row["tmdb_season"] == 1


def test_movie_and_imdb_ids_are_exploded_for_indexing(datasets):
    rows = db.query(
        "SELECT kind, value FROM ds_fribb_ext WHERE anidb_id = 7 ORDER BY kind"
    )
    assert [(r["kind"], r["value"]) for r in rows] == [
        ("imdb", "tt0119698"),
        ("tmdb_movie", "128"),
    ]


def test_missing_mal_id_is_preserved_as_null(datasets):
    """An entry with no MAL id must be visible as such, not defaulted to 0."""
    row = db.query_one("SELECT mal_id FROM ds_fribb WHERE anidb_id = 13430")
    assert row["mal_id"] is None


def test_non_numeric_season_becomes_null_not_zero():
    """Defensive: if upstream ever emits season "a", it must not look like season 0."""
    main, _ = fribb.parse_records(
        [{"anidb_id": 1, "anilist_id": 2, "tvdb_id": 3, "season": {"tvdb": "a"}}]
    )
    assert main[0][5] is None


def test_fribb_refuses_to_replace_a_working_cache_with_nothing(datasets):
    before = fribb.row_count()
    with pytest.raises(ValueError):
        fribb.load([])
    assert fribb.row_count() == before


# --------------------------------------------------------------------------
# community dataset
# --------------------------------------------------------------------------
def test_community_start_is_normalised_to_an_offset(datasets):
    """start: 13 means "this entry begins at episode 13", i.e. offset 12."""
    row = db.query_one(
        "SELECT ep_offset FROM ds_community"
        " WHERE plex_guid = 'plex://show/5d9c08273c3f87001f348b54'"
        "   AND season = 10 AND anilist_id = 101925"
    )
    assert row["ep_offset"] == 12


def test_community_negative_offset_is_kept(datasets):
    """start: 0 means the AniList entry runs one ahead of Plex, so offset -1."""
    row = db.query_one(
        "SELECT ep_offset FROM ds_community WHERE anilist_id = 555001"
    )
    assert row["ep_offset"] == -1


def test_community_entries_without_a_guid_are_dropped(datasets):
    """This project matches on ids; a title-only entry cannot be used safely."""
    row = db.query_one("SELECT COUNT(*) AS n FROM ds_community WHERE anilist_id = 999999")
    assert row["n"] == 0


def test_community_load_is_idempotent(datasets):
    text = (FIXTURES / "community_slice.yaml").read_text(encoding="utf-8")
    first = community.row_count()
    community.load(community.parse_yaml(text, "again.yaml"))
    assert community.row_count() == first


def test_overlapping_source_files_collapse(datasets):
    """The tvdb and tmdb series files list many of the same shows.

    Without deduping, one show would appear twice at the same offset and the
    engine would read that as an ambiguous match.
    """
    text = (FIXTURES / "community_slice.yaml").read_text(encoding="utf-8")
    rows = community.parse_yaml(text, "series-tvdb.en.yaml")
    rows += community.parse_yaml(text, "series-tmdb.en.yaml")
    community.load(rows)
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM ds_community"
        " WHERE plex_guid = 'plex://show/5d9c08273c3f87001f348b54' AND season = 1"
    )
    assert row["n"] == 1
