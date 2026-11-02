"""Process-level configuration.

Deliberately tiny: the only things that come from the environment are the ones
that must be known before the database exists. Everything else is a setting in
the database, editable in the UI, applied without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PORT = _int_env("PORT", 8787)
TZ = os.environ.get("TZ", "UTC")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

DB_PATH = CONFIG_DIR / "bridge.db"
DATASET_CACHE_DIR = CONFIG_DIR / "datasets"

APP_NAME = "plex-anilist-bridge"
APP_VERSION = "0.1.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/666Syner/plex-anilist-bridge)"
