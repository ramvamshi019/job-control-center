"""
services/dedupe.py
------------------
Duplicate detection. Three levels, checked in order:
  1. raw_data_hash  (company + title + location + url)  -> exact same posting from same source
  2. job_url                                            -> same posting, diff hash (re-crawl)
  3. canonical_key  (normalized company + title + location) -> SAME real posting seen via
     a DIFFERENT source (e.g. Stripe req on Greenhouse AND Simplify AND HN Hiring)

Levels 1-2 catch source-scoped dupes; level 3 catches cross-source dupes so the
same real posting isn't scored/ranked/paid-for-by-Claude three times.

`is_duplicate(session, job)` returns True if we already stored this job.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.models.job import Job


def find_duplicate(session: Session, job: Job) -> Job | None:
    """Return the STORED job this one duplicates, or None if it's genuinely new.

    Returns the row rather than a bool so the caller can stamp last_seen_at on
    it — a re-crawl that finds the same posting is proof the req is still open,
    which is the only reliable ghost-job signal we have.
    """
    if job.raw_data_hash:
        existing = session.exec(
            select(Job).where(Job.raw_data_hash == job.raw_data_hash)
        ).first()
        if existing:
            return existing
    if job.job_url:
        existing = session.exec(select(Job).where(Job.job_url == job.job_url)).first()
        if existing:
            return existing
    # Cross-source dedupe: same real posting from a different feed. canonical_key
    # is populated by BaseCrawler.crawl(). Empty key means "not enough signal"
    # (blank title/company) — skip rather than collapse everything with '' key.
    if job.canonical_key:
        existing = session.exec(
            select(Job).where(Job.canonical_key == job.canonical_key)
        ).first()
        if existing:
            return existing
    return None


def is_duplicate(session: Session, job: Job) -> bool:
    """Back-compat wrapper for callers that only need the yes/no."""
    return find_duplicate(session, job) is not None
