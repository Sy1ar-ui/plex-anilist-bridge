"""The setup steps, and whether each is done.

One definition, read from the database every time, used by both the setup
wizard and the dashboard checklist so the two can never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import auth, db, settings_store, tokens
from app.plex import client as plex_client


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    label: str              # the dashboard checklist wording
    done: bool
    optional: bool = False

    @property
    def href(self) -> str:
        return f"/setup/{self.key}"


ORDER = ("password", "plex", "account", "libraries", "anilist", "mal", "webhook")


def steps() -> list[Step]:
    anilist = tokens.get(tokens.ANILIST)
    mal = tokens.get(tokens.MAL)
    enabled = db.query_one("SELECT 1 FROM library WHERE enabled = 1 LIMIT 1")
    webhook = db.query_one("SELECT 1 FROM watermark WHERE name = 'last_webhook'")
    return [
        Step("password", "Admin password", "Set an admin password", auth.password_is_set()),
        Step("plex", "Plex server", "Connect your Plex server", plex_client.stored_server() is not None),
        Step("account", "Your account", "Choose which Plex account counts",
             plex_client.configured_account_id() is not None),
        Step("libraries", "Anime libraries", "Tick your anime libraries", enabled is not None),
        Step("anilist", "AniList", "Connect AniList",
             anilist.present and anilist.status != tokens.STATUS_BROKEN),
        Step("mal", "MyAnimeList", "Connect MyAnimeList (optional)",
             mal.present and mal.status != tokens.STATUS_BROKEN, optional=True),
        Step("webhook", "Plex webhook", "Add the webhook in Plex", webhook is not None),
    ]


def remaining(all_steps: list[Step] | None = None) -> list[Step]:
    """Required steps not done yet."""
    return [s for s in (all_steps or steps()) if not s.done and not s.optional]


def next_step(all_steps: list[Step] | None = None) -> Step | None:
    todo = remaining(all_steps)
    return todo[0] if todo else None


def after(key: str) -> str:
    """The step that follows ``key``, or "done"."""
    index = ORDER.index(key)
    return ORDER[index + 1] if index + 1 < len(ORDER) else "done"


def is_fresh_install() -> bool:
    """Nothing configured and the wizard never finished or dismissed."""
    return not settings_store.get("setup_complete", False) and plex_client.stored_server() is None


def mark_complete() -> None:
    settings_store.set("setup_complete", True)
