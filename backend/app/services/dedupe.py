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


def is_duplicate(session: Session, job: Job) -> bool:
    if job.raw_data_hash:
        existing = session.exec(
            select(Job).where(Job.raw_data_hash == job.raw_data_hash)
        ).first()
        if existing:
            return True
    if job.job_url:
        existing = session.exec(select(Job).where(Job.job_url == job.job_url)).first()
        if existing:
            return True
    return False
