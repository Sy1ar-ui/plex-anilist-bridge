"""AniList GraphQL documents.

Adapted from PlexAniSync <https://github.com/RickDB/PlexAniSync>
(``plexanisync/graphql.py``), with the queries reshaped for per-episode use.

    Copyright (C) 2019-2025 RickDB and PlexAniSync contributors
    Copyright (C) 2026 plex-anilist-bridge contributors

    This program is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by the Free
    Software Foundation, either version 3 of the License, or (at your option)
    any later version. See the LICENSE file at the root of this repository.
"""
from __future__ import annotations

ENDPOINT = "https://graphql.anilist.co"

VIEWER = """
query {
  Viewer {
    id
    name
    avatar { medium }
    mediaListOptions { scoreFormat }
  }
}
"""

# One request gets the media's metadata *and* the authenticated user's list
# entry for it, which is all the rules need. `mediaListEntry` is null when the
# title is not on the list, so "untracked" needs no second round trip.
MEDIA_WITH_ENTRY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    idMal
    episodes
    status
    format
    seasonYear
    title { romaji english }
    coverImage { medium }
    mediaListEntry {
      id
      status
      progress
      repeat
      startedAt { year month day }
      completedAt { year month day }
    }
  }
}
"""

# Every argument is optional: omitting a variable leaves the field untouched.
# Score and notes are deliberately not arguments here, so no code path can
# write them even by accident.
SAVE_ENTRY = """
mutation (
  $mediaId: Int,
  $status: MediaListStatus,
  $progress: Int,
  $repeat: Int,
  $startedAt: FuzzyDateInput,
  $completedAt: FuzzyDateInput
) {
  SaveMediaListEntry(
    mediaId: $mediaId,
    status: $status,
    progress: $progress,
    repeat: $repeat,
    startedAt: $startedAt,
    completedAt: $completedAt
  ) {
    id
    status
    progress
    repeat
    media { id episodes title { romaji english } }
  }
}
"""

SEARCH = """
query ($search: String!, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      idMal
      episodes
      status
      format
      seasonYear
      title { romaji english }
      coverImage { medium }
      mediaListEntry { id status progress }
    }
  }
}
"""

# Used by the AniList -> MAL leg: newest changes first, so polling can stop as
# soon as it reaches the watermark.
RECENT_LIST_CHANGES = """
query ($userId: Int!, $page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage currentPage }
    mediaList(userId: $userId, type: ANIME, sort: UPDATED_TIME_DESC) {
      id
      status
      progress
      repeat
      updatedAt
      score(format: POINT_100)
      startedAt { year month day }
      completedAt { year month day }
      media {
        id
        idMal
        episodes
        title { romaji english }
      }
    }
  }
}
"""
