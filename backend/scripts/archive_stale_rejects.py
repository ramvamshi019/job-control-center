"""
scripts/archive_stale_rejects.py
--------------------------------
Nightly hygiene: move status='Rejected' jobs older than 60 days to
'Archived'. Rejected pool is 753k+ rows and grows constantly — every
LEFT JOIN and daily aggregate query pays for those rows even though
they can't come back. Archived jobs stay in the DB (nothing is deleted)
but drop out of every dashboard query.

Reversible via any manual UPDATE. Cron: 03:30 UTC daily (before backup
at 03:15 wait -- run AFTER backup so the archive is captured).
Actually 03:20 UTC: right AFTER backup.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("archive_stale")

DAYS = int(os.environ.get("ARCHIVE_AFTER_DAYS", "60"))
BATCH = int(os.environ.get("ARCHIVE_BATCH", "20000"))


def run() -> dict:
    cutoff = utcnow_naive() - timedelta(days=DAYS)
    total_archived = 0
    while True:
        with engine.begin() as c:
            c.execute(text("PRAGMA busy_timeout = 30000"))
            res = c.execute(text("""
                UPDATE jobs SET status = 'Archived', updated_at = :ts
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status = 'Rejected' AND discovered_at < :d
                    LIMIT :n
                )
            """), {"ts": utcnow_naive(), "d": cutoff, "n": BATCH})
            n = res.rowcount or 0
        total_archived += n
        log.info("archive_stale: batch=%d total=%d", n, total_archived)
        if n < BATCH:
            break
    log.info("archive_stale: DONE total=%d (cutoff=%s)", total_archived, cutoff)
    return {"archived": total_archived}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
