"""
scripts/daily_export.py
-----------------------
Export approved jobs to exports/daily_jobs.csv. Pair with a daily cron job.

Run from the backend/ folder:
    python scripts/daily_export.py
    python scripts/daily_export.py Approved Applied   # export multiple statuses
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import init_db, session_scope  # noqa: E402
from app.services import exporter  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("daily_export")

# From backend/, the exports folder is one level up at ../exports/.
EXPORT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "exports", "daily_jobs.csv")


def main() -> None:
    init_db()
    statuses = sys.argv[1:] or ["Approved"]
    with session_scope() as session:
        path, count = exporter.export_jobs(session, statuses=statuses, path=EXPORT_PATH)
    log.info("Exported %d job(s) with status %s -> %s", count, statuses, os.path.abspath(path))


if __name__ == "__main__":
    main()
