"""Settings, held in the database and read on every use.

Nothing caches a setting at import time, so a change in the UI takes effect on
the next operation with no restart. Defaults live here, in one place, and are
the documented behaviour from the design.
"""
from __future__ import annotations

import json
from typing import Any

from app import db

DEFAULTS: dict[str, Any] = {
    # --- safety -------------------------------------------------------------
    "dry_run": True,
    # --- Plex -> AniList ----------------------------------------------------
    "sync_enabled": True,
    "skip_specials": True,            # Plex season 0
    "add_untracked": True,            # add titles not yet on the AniList list
    "uncertain_action": "hold",       # hold | apply_and_flag
    "rewatch_action": "ignore",       # ignore | repeating
    "set_dates": True,                # startedAt / completedAt on transitions
    "rate_before_complete": True,     # hold the final episode until rated in the UI
    "reconcile_interval_hours": 6,
    "reconcile_lookback_hours": 72,
    # --- AniList outages and throttling ---------------------------------------
    "anilist_retry_interval_minutes": 5,   # how often deferred events are retried
    "anilist_retry_give_up_days": 7,       # stop retrying an event after this long
    "anilist_max_wait_seconds": 120,       # longest a request queues on the rate limit
    # --- AniList -> MAL -----------------------------------------------------
    "mal_sync_enabled": False,
    "mal_poll_interval_minutes": 15,
    "mal_sync_score": True,
    "mal_sync_dates": False,
    # --- datasets -----------------------------------------------------------
    "dataset_refresh_hours": 24,
    # --- Sonarr (optional; recovers TVDB ids Plex does not give) -------------
    "sonarr_url": "",
    "sonarr_api_key": "",
    "sonarr_refresh_hours": 6,
    "sonarr_last_error": "",
    # --- notifications ------------------------------------------------------
    "notify_webhook_url": "",
    "notify_service": "auto",         # auto | discord | ntfy | json
    "notify_on_review": True,
    "notify_on_rating": True,
    "notify_on_token_error": True,
    "ui_base_url": "",                # where the UI was last opened, for links in notifications
    # --- internal -----------------------------------------------------------
    "webhook_path_token": "",         # generated on first run
    "admin_password_hash": "",        # empty = no password set yet
    "setup_complete": False,
}


def get(key: str, default: Any = None) -> Any:
    row = db.query_one("SELECT value_json FROM setting WHERE key = ?", (key,))
    if row is None:
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default
    try:
        return json.loads(row["value_json"])
    except (ValueError, TypeError):
        return DEFAULTS.get(key, default)


def set(key: str, value: Any) -> None:  # noqa: A001 - deliberate module-level name
    db.execute(
        "INSERT INTO setting (key, value_json) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
        (key, json.dumps(value)),
    )


def set_many(values: dict[str, Any]) -> None:
    with db.session() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO setting (key, value_json) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json",
                (key, json.dumps(value)),
            )


def all_settings() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for row in db.query("SELECT key, value_json FROM setting"):
        try:
            merged[row["key"]] = json.loads(row["value_json"])
        except (ValueError, TypeError):
            continue
    return merged


def is_dry_run() -> bool:
    return bool(get("dry_run"))
