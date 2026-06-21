"""
scripts/crawl_all.py
--------------------
Crawl EVERY active company, ignoring priority intervals. Good for first runs
and testing.

Run from the backend/ folder:
    python scripts/crawl_all.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.services import scheduler  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("crawl_all")


def main() -> None:
    init_db()
    with session_scope() as session:
        companies = session.exec(
            select(Company).where(Company.is_active == True)  # noqa: E712
        ).all()
        if not companies:
            log.warning("No active companies. Run scripts/seed_companies.py first.")
            return
        summaries = scheduler.run_crawl(session, companies)

    total_found = sum(s["found"] for s in summaries)
    total_new = sum(s["new"] for s in summaries)
    total_rejected = sum(s["rejected"] for s in summaries)
    log.info(
        "Done. %d companies | found=%d new=%d rejected=%d",
        len(summaries), total_found, total_new, total_rejected,
    )


if __name__ == "__main__":
    main()
