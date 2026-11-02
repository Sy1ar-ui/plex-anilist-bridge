"""Title normalisation and season-suffix variants.

Derived from PlexAniSync <https://github.com/RickDB/PlexAniSync>, specifically
the ``__clean_title`` normaliser and the season-variant generation in
``plexanisync/anilist.py``.

    Copyright (C) 2019-2025 RickDB and PlexAniSync contributors
    Copyright (C) 2026 plex-anilist-bridge contributors

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version. See the LICENSE file at the root of this repository.

Changes from the original: the ordinal generation is hand-rolled rather than
using ``inflect`` (one fewer dependency), and these helpers feed only the
low-confidence title-search fallback. The ID-based layers never use them.
"""
from __future__ import annotations

import re

# Keep ASCII alphanumerics plus CJK, Bopomofo, Hiragana and Katakana; drop
# everything else, so "Re:Zero -Starting Life-" and "rezero starting life"
# compare equal.
_KEEP = re.compile(
    r"[^a-z0-9"
    r"一-鿿"   # CJK unified ideographs
    r"㄀-ㄯ"   # Bopomofo
    r"぀-ゟ"   # Hiragana
    r"゠-ヿ"   # Katakana
    r"]"
)

_TRAILING_YEAR = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")

_ROMAN = {
    1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii",
    8: "viii", 9: "ix", 10: "x", 11: "xi", 12: "xii",
}


def clean_title(title: str) -> str:
    """Lowercase and strip a title down to comparable characters."""
    if not title:
        return ""
    return _KEEP.sub("", title.strip().lower())


def strip_trailing_year(title: str) -> str:
    return _TRAILING_YEAR.sub("", title or "").strip()


def ordinal(n: int) -> str:
    """2 -> '2nd'. Hand-rolled to avoid a dependency on inflect."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def title_candidates(*titles: str | None) -> list[str]:
    """De-duplicated search candidates for a show, cleaned and uncleaned."""
    out: list[str] = []
    for raw in titles:
        if not raw:
            continue
        base = strip_trailing_year(raw)
        for variant in (raw.strip().lower(), base.lower(), clean_title(base)):
            if variant and variant not in out:
                out.append(variant)
    return out


def season_variants(title: str, season: int) -> list[str]:
    """Plausible AniList titles for season N of a show.

    Mirrors PlexAniSync's suffix list: roman numeral, "season N", "part N",
    a bare number, and ordinal forms.
    """
    base = strip_trailing_year(title or "").strip()
    if not base or season <= 1:
        return [base] if base else []
    suffixes = [
        _ROMAN.get(season, str(season)),
        f"season {season}",
        f"part {season}",
        str(season),
        f"{ordinal(season)} season",
        f"the {ordinal(season)} season",
    ]
    out: list[str] = []
    for suffix in suffixes:
        variant = f"{base} {suffix}"
        if variant not in out:
            out.append(variant)
    return out
