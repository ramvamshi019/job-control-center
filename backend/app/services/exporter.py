"""
services/exporter.py
--------------------
Export jobs to CSV for manual apply or a human assistant.

`export_jobs(session, statuses, path)` writes a CSV and returns the path + count.
By default it exports "Approved" jobs to exports/daily_jobs.csv.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("exporter")

# Path is relative to where you run the script (project root recommended).
DEFAULT_EXPORT_PATH = os.path.join("..", "exports", "daily_jobs.csv")

COLUMNS = [
    "id", "title", "company_name", "location", "employment_type",
    "match_score", "sponsorship_risk", "status", "fit_reason", "risk_reason",
    "job_url", "source", "posted_at", "discovered_at",
]


def export_jobs(
    session: Session,
    statuses: Optional[List[str]] = None,
    path: str = DEFAULT_EXPORT_PATH,
) -> Tuple[str, int]:
    statuses = statuses or ["Approved"]
    rows = session.exec(
        select(Job).where(Job.status.in_(statuses)).order_by(Job.match_score.desc())
    ).all()

    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for job in rows:
            writer.writerow({c: _stringify(getattr(job, c, "")) for c in COLUMNS})

    log.info("Exported %d job(s) -> %s", len(rows), path)
    return path, len(rows)


def _stringify(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    return "" if value is None else str(value)
