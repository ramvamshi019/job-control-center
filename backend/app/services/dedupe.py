"""
services/dedupe.py
------------------
Duplicate detection. Two levels:
  1. raw_data_hash  (company + title + location + url)  -> exact dupes
  2. job_url                                            -> same posting, diff hash

`is_duplicate(session, job)` returns True if we already stored this job.
This keeps the DB clean when you crawl the same companies repeatedly.
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
    return None


def is_duplicate(session: Session, job: Job) -> bool:
    """Back-compat wrapper for callers that only need the yes/no."""
    return find_duplicate(session, job) is not None
