"""
scripts/crawl_priority.py
-------------------------
Crawl only companies that are DUE based on their priority interval
(high=30min, medium=3h, low=24h, skip=never). This is what you'd run on a
cron / launchd schedule so you respect each site politely.

Run from the backend/ folder:
    python scripts/crawl_priority.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import init_db, session_scope  # noqa: E402
from app.services import scheduler  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("crawl_priority")


def main() -> None:
    init_db()
    with session_scope() as session:
        companies = scheduler.due_companies(session)
        if not companies:
            log.info("No companies are due right now. Nothing to do.")
            return
        log.info("%d companies due for re-check.", len(companies))
        summaries = scheduler.run_crawl(session, companies)

    total_new = sum(s["new"] for s in summaries)
    log.info("Priority crawl done. %d new jobs across %d companies.", total_new, len(summaries))


if __name__ == "__main__":
    main()
