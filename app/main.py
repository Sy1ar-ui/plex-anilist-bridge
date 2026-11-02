"""Application entry point."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth, config, db, repo, settings_store, tokens
from app.datasets import refresh as dataset_refresh
from app.logging_setup import setup_logging
from app.web import routes_mal, routes_setup, routes_ui, routes_webhook

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


def _restrict_permissions(path: Path) -> None:
    """The database holds account tokens, so keep it owner-only.

    This is not encryption and does not pretend to be: a key stored next to the
    database in the same directory would protect against nothing. Treat
    /config as sensitive.
    """
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        with contextlib.suppress(OSError):
            if candidate.exists():
                os.chmod(candidate, 0o600)


async def _initial_dataset_load() -> None:
    try:
        results = await dataset_refresh.refresh_all()
        for result in results:
            log.info(
                "initial dataset load",
                extra={"dataset": result.name, "status": result.status, "rows": result.rows},
            )
    except Exception:  # noqa: BLE001 - startup must not fail on a network problem
        log.exception("initial dataset load failed; the bridge will use any cache it has")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    db.set_db_path(config.DB_PATH)

    applied = db.migrate()
    if applied:
        log.info("database ready", extra={"migrations_applied": applied})
    _restrict_permissions(config.DB_PATH)

    # Generated once, then stable: the webhook URL must not change under the user.
    auth.webhook_path_token()

    from app import scheduler

    scheduler.start()

    task: asyncio.Task | None = None
    if dataset_refresh.needs_initial_load():
        log.info("no mapping datasets cached yet; fetching in the background")
        task = asyncio.create_task(_initial_dataset_load())

    log.info(
        "started",
        extra={
            "version": config.APP_VERSION,
            "port": config.PORT,
            "dry_run": settings_store.is_dry_run(),
            "config_dir": str(config.CONFIG_DIR),
        },
    )
    try:
        yield
    finally:
        from app import notify

        # Give any notification still being sent a moment to finish.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(notify.drain(), timeout=5)
        scheduler.shutdown()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="Plex AniList Bridge",
        version=config.APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_webhook.router)
    app.include_router(routes_ui.router)
    app.include_router(routes_setup.router)
    app.include_router(routes_mal.router)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Unauthenticated, cheap, and used by the Docker HEALTHCHECK."""
        problems: list[str] = []

        try:
            db.query_one("SELECT 1")
            database_ok = True
        except Exception as exc:  # noqa: BLE001
            database_ok = False
            problems.append(f"database: {exc}")

        from app.plex import client as plex_client

        plex_ready = plex_client.stored_server() is not None
        account_chosen = plex_client.configured_account_id() is not None
        anilist = tokens.get(tokens.ANILIST)

        if not plex_ready:
            problems.append("Plex server not configured")
        if not account_chosen:
            problems.append("no Plex account chosen")
        if anilist.status == tokens.STATUS_BROKEN:
            problems.append("AniList token is broken")

        from app.anilist.client import health as anilist_health
        from app.anilist.client import limiter as anilist_limiter

        limiter = anilist_limiter()
        api = anilist_health().snapshot(limit=limiter.limit, remaining=limiter.remaining)
        deferred = repo.deferred_count() if database_ok else 0
        if api["state"] in ("down", "throttled"):
            problems.append(f"AniList API {api['state']}: {api['last_error'] or api['summary']}")
        if deferred:
            problems.append(f"{deferred} event(s) deferred until AniList is available")

        body = {
            # The process being up is what a healthcheck should report; an
            # unfinished setup is not a crash, so it does not fail the check.
            "status": "ok" if database_ok else "error",
            "version": config.APP_VERSION,
            "dry_run": settings_store.is_dry_run(),
            "database": "ok" if database_ok else "error",
            "setup": {
                "plex_server": plex_ready,
                "plex_account": account_chosen,
                "anilist": anilist.status,
            },
            "events": repo.event_count(),
            "pending_review": repo.pending_review_count(),
            "deferred": deferred,
            # An AniList outage is reported, but does not fail the healthcheck:
            # the container is fine, and restarting it would not help.
            "anilist_api": {
                key: api[key]
                for key in (
                    "state",
                    "retry_at",
                    "last_error",
                    "last_success_at",
                    "limit_per_minute",
                    "throttles_last_10_min",
                )
            },
            "datasets": dataset_refresh.status(),
            "notes": problems,
        }
        return JSONResponse(body, status_code=200 if database_ok else 503)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    setup_logging()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - a container needs to listen on all interfaces
        port=config.PORT,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
