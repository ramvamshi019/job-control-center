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

from sqlalchemy import and_, func, or_
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


def sponsor_ids_subquery():
    """SELECT of confirmed-H-1B-sponsor company ids, kept as a SUBQUERY rather
    than a materialised Python set: there are ~6.7k sponsors and binding that
    many ids as SQL parameters risks SQLITE_MAX_VARIABLE_NUMBER. This way the
    comparison stays inside SQLite."""
    from app.models.company import Company
    return select(Company.id).where(
        Company.h1b_history_score >= settings.sponsor_score_threshold)


def prune_old_jobs(session: Session, days: Optional[int] = None) -> int:
    """Delete stale, non-actioned jobs. Returns how many were removed.

    Two-tier: confirmed sponsors keep settings.sponsor_prune_days, everything
    else keeps settings.prune_days. An explicit `days` overrides BOTH, so a
    manual `--days N` still means exactly N.
    """
    cutoff = stale_cutoff(days)
    if days is not None:
        stmt = delete(Job).where(Job.posted_at < cutoff, Job.status.notin_(PROTECTED))
        sel = select(func.count()).select_from(Job).where(
            Job.posted_at < cutoff, Job.status.notin_(PROTECTED))
    else:
        sponsor_cutoff = stale_cutoff(settings.sponsor_prune_days)
        sponsors = sponsor_ids_subquery()
        # Sponsor rows survive until the LONG cutoff; everything else until the
        # short one. Expressed as a single predicate so the delete stays one
        # statement rather than a per-row loop over ~400k jobs.
        cond = or_(
            and_(Job.company_id.in_(sponsors), Job.posted_at < sponsor_cutoff),
            and_(Job.company_id.notin_(sponsors), Job.posted_at < cutoff),
            and_(Job.company_id.is_(None), Job.posted_at < cutoff),
        )
        stmt = delete(Job).where(cond, Job.status.notin_(PROTECTED))
        sel = select(func.count()).select_from(Job).where(cond, Job.status.notin_(PROTECTED))

    # COUNT, not a materialised row list: this runs every crawl cycle on a
    # 2-vCPU box, and loading ~200k full Job rows into Python just to len() them
    # was burning CPU and RAM the API needs.
    n = session.exec(sel).one()
    if n:
        session.exec(stmt)
        session.commit()
        log.info("Pruned %d jobs (retention: %dd, sponsors %dd).", n,
                 days if days is not None else settings.prune_days,
                 days if days is not None else settings.sponsor_prune_days)
    return n
