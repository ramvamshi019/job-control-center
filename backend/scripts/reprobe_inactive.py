"""
scripts/reprobe_inactive.py
---------------------------
Weekly re-probe of PAUSED companies (is_active=0).

We took the ~14k zero-yield boards off the live crawler's hot path so the
productive companies re-crawl every ~15 min (fresh enough to beat aggregators).
This script periodically re-checks the paused set and REACTIVATES any company
that has started hiring (now returns >=1 job) — so we never permanently miss a
company that begins posting. Run it weekly.

Run from backend/:
    python scripts/reprobe_inactive.py            # full re-probe
    python scripts/reprobe_inactive.py --limit 20 # smoke test on a few
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import update as sa_update  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import scheduler  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("reprobe_inactive")


def main(limit: int = 0, batch: int = 400, workers: int = 16) -> None:
    init_db()
    with session_scope() as session:
        q = select(Company).where(Company.is_active == False)  # noqa: E712
        if limit:
            q = q.limit(limit)
        inactive_ids = [c.id for c in session.exec(q).all()]

    log.info("Re-probing %d paused companies (batch=%d, workers=%d)…",
             len(inactive_ids), batch, workers)
    reactivated = 0

    for i in range(0, len(inactive_ids), batch):
        chunk_ids = inactive_ids[i:i + batch]
        # 1) Crawl + persist any jobs found (full pipeline) for this chunk.
        with session_scope() as session:
            companies = session.exec(
                select(Company).where(Company.id.in_(chunk_ids))).all()
            scheduler.run_crawl_parallel(session, companies, workers=workers)
        # 2) Reactivate any paused company that now has at least one job.
        with session_scope() as session:
            res = session.execute(
                sa_update(Company)
                .where(
                    Company.is_active == False,  # noqa: E712
                    Company.id.in_(chunk_ids),
                    Company.id.in_(
                        select(Job.company_id).where(Job.company_id.is_not(None))
                    ),
                )
                .values(is_active=True, priority="low")
            )
            reactivated += res.rowcount or 0
        log.info("…probed %d/%d, reactivated=%d",
                 min(i + batch, len(inactive_ids)), len(inactive_ids), reactivated)

    log.info("Re-probe done. probed=%d reactivated=%d", len(inactive_ids), reactivated)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="probe only N companies (0 = all)")
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    main(limit=args.limit, batch=args.batch, workers=args.workers)
