"""Title normalisation, ported from PlexAniSync.

These feed only the low-confidence title-search fallback; the ID layers never
use them. They are tested anyway because a wrong normaliser turns a near miss
into a confident-looking wrong answer.
"""
from __future__ import annotations

import pytest

from app.mapping.titles import (
    clean_title,
    ordinal,
    season_variants,
    strip_trailing_year,
    title_candidates,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Re:ZERO -Starting Life in Another World-", "rezero starting life in another world"),
        ("K-On!", "kon"),
        ("Fate/Zero", "fatezero"),
        ("YOUR NAME.", "your name"),
        ("  Spaced   Out  ", "spacedout"),
    ],
)
def test_punctuation_and_case_are_ignored(left, right):
    assert clean_title(left) == clean_title(right)


def test_japanese_characters_are_preserved():
    """Kana and kanji survive; CJK punctuation does not. Escapes, not literals,
    so the test cannot be broken by a file-encoding accident."""
    kana = "けいおん"            # keion, in hiragana
    assert clean_title(kana + "!") == kana
    katakana = "レイト"              # reito, in katakana
    assert clean_title(katakana + " 2") == katakana + "2"
    kanji = "君の名は"           # kimi no na wa
    assert clean_title(kanji + "。") == kanji  # trailing ideographic full stop
    assert clean_title("Kimi no Na wa.") == "kiminonawa"


def test_unsupported_scripts_are_dropped_which_is_a_known_limitation():
    """Cyrillic and similar are stripped entirely, inherited from PlexAniSync's
    character set. Harmless here because titles only feed the low-confidence
    fallback, but worth pinning so the behaviour is not a surprise."""
    assert clean_title("рум") == ""


def test_distinct_titles_stay_distinct():
    assert clean_title("Gintama") != clean_title("Gin Tama 2")
    assert clean_title("Monster") != clean_title("Monogatari")


def test_empty_input_is_safe():
    assert clean_title("") == ""
    assert clean_title(None) == ""


def test_trailing_year_is_stripped():
    assert strip_trailing_year("Shaman King (2021)") == "Shaman King"
    assert strip_trailing_year("2001: A Space Odyssey") == "2001: A Space Odyssey"


@pytest.mark.parametrize(
    ("number", "expected"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
     (13, "13th"), (21, "21st"), (22, "22nd"), (103, "103rd")],
)
def test_ordinals(number, expected):
    assert ordinal(number) == expected


def test_season_variants_cover_the_usual_naming():
    variants = season_variants("Overlord", 2)
    assert "Overlord II" .lower() in [v.lower() for v in variants]
    assert "Overlord season 2" in variants
    assert "Overlord part 2" in variants
    assert "Overlord 2nd season" in variants


def test_season_one_needs_no_suffix():
    assert season_variants("Overlord", 1) == ["Overlord"]


def test_season_variants_drop_a_trailing_year():
    assert all("(2021)" not in v for v in season_variants("Shaman King (2021)", 2))


def test_title_candidates_are_deduplicated():
    candidates = title_candidates("Gintama", "Gintama", None, "")
    assert len(candidates) == len(set(candidates))
    assert "gintama" in candidates
