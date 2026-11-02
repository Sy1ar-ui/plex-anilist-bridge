"""Fetching and caching of the mapping datasets.

Design rules:

* Datasets are cached on disk under ``/config/datasets`` and in the database.
  If GitHub is unreachable the bridge keeps working on the cached copy -- a
  failed refresh never empties a working dataset.
* Conditional GETs (``If-None-Match``) mean the daily refresh usually costs two
  tiny 304 responses.
* The swap happens inside one transaction, so a reader never observes a
  half-loaded table.
* Manual overrides live in a separate table that is consulted first, so a
  dataset refresh can never clobber a correction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from app import config, db
from app.datasets import community, fribb

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=120.0)


@dataclass
class RefreshOutcome:
    name: str
    status: str          # updated | unchanged | stale-cache | error
    rows: int
    detail: str


def cache_dir() -> Path:
    path = config.DATASET_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(filename: str) -> Path:
    return cache_dir() / filename


def _write_cache(filename: str, raw: bytes) -> None:
    target = _cache_path(filename)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(target)


def _stored_etag(name: str) -> str | None:
    row = db.query_one("SELECT etag FROM dataset_meta WHERE name = ?", (name,))
    return row["etag"] if row else None


def _record(name: str, *, etag: str | None, rows: int, status: str, error: str | None) -> None:
    db.execute(
        "INSERT INTO dataset_meta (name, etag, fetched_at, row_count, status, last_error)"
        " VALUES (?, ?, datetime('now'), ?, ?, ?)"
        " ON CONFLICT (name) DO UPDATE SET"
        "   etag = excluded.etag, fetched_at = excluded.fetched_at,"
        "   row_count = excluded.row_count, status = excluded.status,"
        "   last_error = excluded.last_error",
        (name, etag, rows, status, error),
    )


async def _conditional_get(
    client: httpx.AsyncClient, url: str, etag: str | None
) -> tuple[int, bytes | None, str | None]:
    headers = {"User-Agent": config.USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    response = await client.get(url, headers=headers, timeout=_TIMEOUT)
    if response.status_code == 304:
        return 304, None, etag
    response.raise_for_status()
    return response.status_code, response.content, response.headers.get("ETag")


# --------------------------------------------------------------------------
# Fribb
# --------------------------------------------------------------------------
async def refresh_fribb(client: httpx.AsyncClient, *, force: bool = False) -> RefreshOutcome:
    name = fribb.NAME
    cached = _cache_path(fribb.CACHE_FILENAME)
    etag = None if force else _stored_etag(name)

    try:
        status, raw, new_etag = await _conditional_get(client, fribb.URL, etag)
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same here
        return _fallback_to_cache(name, cached, exc, _load_fribb_file)

    if status == 304 and fribb.row_count() > 0:
        _record(name, etag=etag, rows=fribb.row_count(), status="ok", error=None)
        return RefreshOutcome(name, "unchanged", fribb.row_count(), "not modified upstream")

    if raw is not None:
        _write_cache(fribb.CACHE_FILENAME, raw)

    try:
        rows = _load_fribb_file(cached)
    except Exception as exc:  # noqa: BLE001
        _record(name, etag=etag, rows=fribb.row_count(), status="error", error=str(exc))
        return RefreshOutcome(name, "error", fribb.row_count(), f"could not parse: {exc}")

    _record(name, etag=new_etag, rows=rows, status="ok", error=None)
    return RefreshOutcome(name, "updated", rows, f"{rows} entries")


def _load_fribb_file(path: Path) -> int:
    return fribb.load(fribb.parse_bytes(path.read_bytes()))


# --------------------------------------------------------------------------
# community
# --------------------------------------------------------------------------
async def refresh_community(
    client: httpx.AsyncClient, *, force: bool = False
) -> RefreshOutcome:
    name = community.NAME
    changed = False
    errors: list[str] = []

    for filename in community.FILES:
        meta_name = f"{name}/{filename}"
        etag = None if force else _stored_etag(meta_name)
        try:
            status, raw, new_etag = await _conditional_get(
                client, community.url_for(filename), etag
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{filename}: {exc}")
            continue
        if status != 304 and raw is not None:
            _write_cache(filename, raw)
            changed = True
            _record(meta_name, etag=new_etag, rows=0, status="ok", error=None)
        else:
            _record(meta_name, etag=etag, rows=0, status="ok", error=None)

    present = [f for f in community.FILES if _cache_path(f).exists()]
    if not present:
        detail = "; ".join(errors) or "no cached files"
        _record(name, etag=None, rows=0, status="error", error=detail)
        return RefreshOutcome(name, "error", 0, detail)

    if not changed and community.row_count() > 0 and not force:
        rows = community.row_count()
        _record(name, etag=None, rows=rows, status="ok", error=None)
        return RefreshOutcome(name, "unchanged", rows, "not modified upstream")

    try:
        rows = _load_community_files(present)
    except Exception as exc:  # noqa: BLE001
        _record(name, etag=None, rows=community.row_count(), status="error", error=str(exc))
        return RefreshOutcome(name, "error", community.row_count(), f"could not parse: {exc}")

    status_name = "updated" if not errors else "stale-cache"
    detail = f"{rows} mappings from {len(present)} file(s)"
    if errors:
        detail += f"; using cache for: {'; '.join(errors)}"
    _record(name, etag=None, rows=rows, status="ok", error="; ".join(errors) or None)
    return RefreshOutcome(name, status_name, rows, detail)


def _load_community_files(filenames: list[str]) -> int:
    rows: list[community.Row] = []
    for filename in filenames:
        text = _cache_path(filename).read_text(encoding="utf-8")
        rows.extend(community.parse_yaml(text, filename))
    return community.load(rows)


# --------------------------------------------------------------------------
# shared fallback
# --------------------------------------------------------------------------
def _fallback_to_cache(name: str, cached: Path, exc: Exception, loader) -> RefreshOutcome:
    """A download failed. Keep serving, and say so plainly."""
    existing = fribb.row_count() if name == fribb.NAME else community.row_count()
    if existing > 0:
        _record(name, etag=_stored_etag(name), rows=existing, status="stale", error=str(exc))
        return RefreshOutcome(
            name, "stale-cache", existing,
            f"download failed ({exc}); still using the {existing} cached entries",
        )
    if cached.exists():
        try:
            rows = loader(cached)
        except Exception as inner:  # noqa: BLE001
            _record(name, etag=None, rows=0, status="error", error=str(inner))
            return RefreshOutcome(name, "error", 0, f"download and cache both failed: {inner}")
        _record(name, etag=None, rows=rows, status="stale", error=str(exc))
        return RefreshOutcome(
            name, "stale-cache", rows, f"download failed ({exc}); loaded {rows} from disk cache"
        )
    _record(name, etag=None, rows=0, status="error", error=str(exc))
    return RefreshOutcome(name, "error", 0, f"download failed and there is no cache: {exc}")


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------
async def refresh_all(*, force: bool = False) -> list[RefreshOutcome]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = [
            await refresh_fribb(client, force=force),
            await refresh_community(client, force=force),
        ]
    for result in results:
        log.info(
            "dataset refresh",
            extra={"dataset": result.name, "status": result.status, "rows": result.rows},
        )
    return results


def status() -> list[dict]:
    rows = db.query(
        "SELECT name, fetched_at, row_count, status, last_error FROM dataset_meta"
        " WHERE name IN (?, ?) ORDER BY name",
        (fribb.NAME, community.NAME),
    )
    return [dict(r) for r in rows]


def needs_initial_load() -> bool:
    return fribb.row_count() == 0 and community.row_count() == 0
