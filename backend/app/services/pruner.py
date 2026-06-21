"""
services/pruner.py
------------------
Retention policy: keep the database light by removing stale postings.

`prune_old_jobs` deletes jobs whose posted_at is older than settings.prune_days,
EXCEPT jobs you've already actioned (Approved / Applied / Follow-up) — those are
your decisions and are always kept.

`is_stale` is used by the crawler so a too-old posting is never re-inserted after
being pruned (avoids delete/re-add churn every cycle).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, delete, select

from app.config import settings
from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("pruner")

# Statuses that are never auto-deleted regardless of age.
PROTECTED = ("Approved", "Applied", "Follow-up")


def stale_cutoff(days: Optional[int] = None) -> datetime:
    return datetime.utcnow() - timedelta(days=days if days is not None else settings.prune_days)


def is_stale(job: Job, cutoff: Optional[datetime] = None) -> bool:
    """True if the posting is older than the retention window."""
    cutoff = cutoff or stale_cutoff()
    return bool(job.posted_at and job.posted_at < cutoff)


def prune_old_jobs(session: Session, days: Optional[int] = None) -> int:
    """Delete stale, non-actioned jobs. Returns how many were removed."""
    cutoff = stale_cutoff(days)
    to_delete = session.exec(
        select(Job).where(Job.posted_at < cutoff, Job.status.notin_(PROTECTED))
    ).all()
    n = len(to_delete)
    if n:
        session.exec(
            delete(Job).where(Job.posted_at < cutoff, Job.status.notin_(PROTECTED))
        )
        session.commit()
        log.info("Pruned %d jobs older than %d days.", n, days or settings.prune_days)
    return n
