"""Sonarr, optionally, as a source of TVDB ids Plex did not give.

Some Plex libraries carry no TVDB id for a show: a TMDB-only or legacy agent,
or "Personal Media". Without it the TVDB-keyed mapping layers cannot run.
Sonarr nearly always knows the TVDB id, so the bridge keeps a copy of
Sonarr's series list (:func:`refresh`, on a schedule) and the mapping engine
looks the show up there (:func:`lookup`) -- offline, like the datasets.

The lookup is by evidence, strongest first:

1. **imdb / tmdb** -- Plex gave one of these ids, and one Sonarr series has it;
2. **folder** -- the show's folder name in Plex equals one Sonarr series'
   folder name (mount points differ between containers; names do not);
3. **title** -- the cleaned title (and year, when both have one) equals one
   series' title or alternate title. A title is a guess, so a match found
   this way is only ever *uncertain* and held for review, as with the AniList
   title search.

Two Sonarr series answering the same evidence means no recovery: it is
ambiguous, and the reason is recorded in the trace. Nothing is ever written
to Sonarr.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app import db, settings_store
from app.mapping.titles import clean_title
from app.models import ShowIds

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(30.0, connect=5.0)
BASIS_UNCERTAIN = {"title"}


class SonarrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Recovery:
    tvdb_id: int
    basis: str          # imdb | tmdb | folder | title
    title: str

    @property
    def certain(self) -> bool:
        return self.basis not in BASIS_UNCERTAIN

    def describe(self) -> str:
        how = {"imdb": "by IMDb id", "tmdb": "by TMDB id", "folder": "by folder name",
               "title": "by title, which is a guess"}[self.basis]
        return f'Sonarr recovered TVDB {self.tvdb_id} for "{self.title}" {how}'


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def configured() -> tuple[str, str] | None:
    url = str(settings_store.get("sonarr_url", "") or "").strip().rstrip("/")
    key = str(settings_store.get("sonarr_api_key", "") or "").strip()
    if not url or not key or urlparse(url).scheme not in ("http", "https"):
        return None
    return url, key


# --------------------------------------------------------------------------
# the API (read only)
# --------------------------------------------------------------------------
async def _get(url: str, key: str, path: str, *, http: httpx.AsyncClient | None = None) -> Any:
    owned = http is None
    client = http or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        response = await client.get(f"{url}/api/v3/{path}", headers={"X-Api-Key": key, "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SonarrError(f"Sonarr could not be reached at {url}: {exc or exc.__class__.__name__}") from exc
    finally:
        if owned:
            await client.aclose()
    if response.status_code == 401:
        raise SonarrError("Sonarr rejected the API key")
    if response.status_code >= 400:
        raise SonarrError(f"Sonarr answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise SonarrError("Sonarr did not answer with JSON; check the URL (including any URL base)") from exc


async def check(url: str, key: str, *, http: httpx.AsyncClient | None = None) -> str:
    """The Sonarr version, if the URL and key work."""
    data = await _get(url.rstrip("/"), key, "system/status", http=http)
    return str((data or {}).get("version") or "unknown version")


async def refresh(*, http: httpx.AsyncClient | None = None) -> int:
    """Replace the local copy of Sonarr's series list. Returns how many series."""
    conf = configured()
    if conf is None:
        raise SonarrError("Sonarr is not set up")
    data = await _get(*conf, "series", http=http)
    if not isinstance(data, list):
        raise SonarrError("Sonarr returned something other than a series list")
    rows = []
    for item in data:
        if not item.get("id"):
            continue
        alternates = sorted({
            clean_title(a.get("title") or "") for a in item.get("alternateTitles") or []
        } - {""})
        rows.append((
            int(item["id"]), item.get("title"), clean_title(item.get("title") or ""),
            item.get("year") or None, item.get("tvdbId") or None, item.get("tmdbId") or None,
            (item.get("imdbId") or None), _folder(item.get("path")), json.dumps(alternates),
        ))
    with db.session() as conn:
        conn.execute("DELETE FROM sonarr_series")
        conn.executemany(
            "INSERT INTO sonarr_series (sonarr_id, title, clean_title, year, tvdb_id, tmdb_id, imdb_id,"
            " folder, alt_titles, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            rows,
        )
    settings_store.set("sonarr_last_error", "")
    log.info("Sonarr series refreshed", extra={"series": len(rows)})
    return len(rows)


def _folder(path: str | None) -> str | None:
    if not path:
        return None
    name = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip().lower()
    return name or None


def status() -> dict[str, Any]:
    row = db.query_one("SELECT COUNT(*) AS n, MAX(refreshed_at) AS at FROM sonarr_series")
    return {
        "configured": configured() is not None,
        "series": int(row["n"]) if row else 0,
        "refreshed_at": row["at"] if row else None,
        "last_error": settings_store.get("sonarr_last_error", "") or None,
    }


# --------------------------------------------------------------------------
# the lookup the mapping engine uses (offline)
# --------------------------------------------------------------------------
def lookup(show: ShowIds) -> tuple[Recovery | None, str]:
    """Find this show's TVDB id in the cached Sonarr list. Returns (recovery, why)."""
    if not db.query_one("SELECT 1 FROM sonarr_series LIMIT 1"):
        return None, "no Sonarr series list (Sonarr not set up, or not refreshed yet)"

    def one(sql: str, args: tuple, basis: str) -> tuple[Recovery | None, str | None]:
        found = db.query(f"SELECT tvdb_id, title FROM sonarr_series WHERE tvdb_id IS NOT NULL AND {sql}", args)
        ids = {int(r["tvdb_id"]) for r in found}
        if len(ids) == 1:
            return Recovery(ids.pop(), basis, found[0]["title"] or ""), None
        if len(ids) > 1:
            names = ", ".join(sorted({r["title"] or "?" for r in found}))
            return None, f"Sonarr has {len(ids)} series matching by {basis} ({names}), so this needs a human"
        return None, None

    tried: list[str] = []
    if show.imdb_id:
        tried.append(f"imdb {show.imdb_id}")
        hit, problem = one("imdb_id = ?", (show.imdb_id,), "imdb")
        if hit or problem:
            return hit, problem or hit.describe()
    if show.tmdb_id:
        tried.append(f"tmdb {show.tmdb_id}")
        hit, problem = one("tmdb_id = ?", (int(show.tmdb_id),), "tmdb")
        if hit or problem:
            return hit, problem or hit.describe()
    if show.folder:
        tried.append(f'folder "{show.folder}"')
        hit, problem = one("folder = ?", (show.folder.strip().lower(),), "folder")
        if hit or problem:
            return hit, problem or hit.describe()
    if show.title:
        wanted = clean_title(show.title)
        tried.append(f'title "{show.title}"')
        candidates = []
        for row in db.query("SELECT tvdb_id, title, clean_title, year, alt_titles FROM sonarr_series"
                            " WHERE tvdb_id IS NOT NULL"):
            names = {row["clean_title"], *json.loads(row["alt_titles"] or "[]")}
            if wanted and wanted in names and (not show.year or not row["year"] or int(row["year"]) == int(show.year)):
                candidates.append(row)
        ids = {int(r["tvdb_id"]) for r in candidates}
        if len(ids) == 1:
            hit = Recovery(ids.pop(), "title", candidates[0]["title"] or "")
            return hit, hit.describe()
        if len(ids) > 1:
            names = ", ".join(sorted({r["title"] or "?" for r in candidates}))
            return None, f"Sonarr has {len(ids)} series titled like this ({names}), so this needs a human"
    return None, "Sonarr has no series matching " + (", ".join(tried) if tried else "anything Plex gave")
