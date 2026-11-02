"""Background jobs, rescheduled from settings without a restart.

APScheduler is used specifically because intervals can be changed at runtime:
saving a new interval in the UI calls :func:`apply_settings`, which reschedules
the live job. Nothing here requires the container to be restarted.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import settings_store
from app.datasets import refresh as dataset_refresh
from app.sync import reconcile

log = logging.getLogger(__name__)

JOB_DATASETS = "dataset_refresh"
JOB_RECONCILE = "plex_reconcile"

_scheduler: AsyncIOScheduler | None = None


async def _dataset_job() -> None:
    try:
        await dataset_refresh.refresh_all()
    except Exception:  # noqa: BLE001 - a scheduled job must never die silently
        log.exception("dataset refresh job failed")


async def _reconcile_job() -> None:
    try:
        await reconcile.run_reconcile()
    except Exception:  # noqa: BLE001
        log.exception("reconcile job failed")


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    settings = settings_store.all_settings()

    scheduler.add_job(
        _dataset_job,
        IntervalTrigger(hours=max(1, int(settings.get("dataset_refresh_hours", 24)))),
        id=JOB_DATASETS,
        name="Refresh mapping datasets",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _reconcile_job,
        IntervalTrigger(hours=max(1, int(settings.get("reconcile_interval_hours", 6)))),
        id=JOB_RECONCILE,
        name="Reconcile against Plex history",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler started", extra={"jobs": [j.id for j in scheduler.get_jobs()]})
    return scheduler


def apply_settings() -> None:
    """Re-read intervals from the database and reschedule the live jobs."""
    if _scheduler is None:
        return
    settings = settings_store.all_settings()
    _reschedule(
        JOB_DATASETS, hours=max(1, int(settings.get("dataset_refresh_hours", 24)))
    )
    _reschedule(
        JOB_RECONCILE, hours=max(1, int(settings.get("reconcile_interval_hours", 6)))
    )


def _reschedule(job_id: str, **interval) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.reschedule_job(job_id, trigger=IntervalTrigger(**interval))
        log.info("job rescheduled", extra={"job": job_id, **interval})
    except Exception:  # noqa: BLE001
        log.warning("could not reschedule job", extra={"job": job_id})


def run_now(job_id: str) -> bool:
    """Kick a job immediately, leaving its schedule alone."""
    if _scheduler is None:
        return False
    job = _scheduler.get_job(job_id)
    if job is None:
        return False
    import datetime as _dt

    job.modify(next_run_time=_dt.datetime.now(_dt.UTC))
    return True


def job_states() -> list[dict]:
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in _scheduler.get_jobs()
    ]


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
