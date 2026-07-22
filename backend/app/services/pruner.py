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


def prune_ghost_jobs(session: Session, days: Optional[int] = None) -> int:
    """Delete postings we haven't SEEN on the employer's board in `days`.

    This is the real ghost-job filter, and it does what age-based retention
    can't. 43% of stored jobs (iCIMS, SmartRecruiters) have no posted_at at
    all, so `prune_old_jobs` can never expire them no matter how dead they are.
    A filled req simply stops appearing in the board feed, and last_seen_at
    stops advancing — that's the signal.

    Deliberately generous by default (settings.ghost_days): a job must be
    missing across MANY crawl cycles before it's dropped, so one failed fetch,
    a rate-limit, or a paginated crawler returning a partial page can never
    delete live jobs. Actioned jobs are protected as always.
    """
    cutoff = stale_cutoff(days if days is not None else settings.ghost_days)
    cond = and_(Job.last_seen_at.is_not(None), Job.last_seen_at < cutoff)
    n = session.exec(
        select(func.count()).select_from(Job).where(cond, Job.status.notin_(PROTECTED))
    ).one()
    if n:
        session.exec(delete(Job).where(cond, Job.status.notin_(PROTECTED)))
        session.commit()
        log.info("Pruned %d ghost jobs (not seen on their board in %dd).",
                 n, days if days is not None else settings.ghost_days)
    return n


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
