"""
scripts/reclassify_messages.py
------------------------------
Re-run gmail_watcher.classify() on every row in job_messages using the
CURRENT classifier logic. Run this after tightening the rules to fix
mis-tagged historical rows without waiting for new mail to trickle in.

    docker exec -w /app/backend job-control-center-backend-1 \
        python scripts/reclassify_messages.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402
from gmail_watcher import classify  # noqa: E402

log = get_logger("reclassify")


def run() -> int:
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text(
            "SELECT id, subject, snippet, classification FROM job_messages"
        )).all()
        changed = 0
        stats = {"interview": 0, "rejection": 0, "ack": 0, "other": 0}
        for id_, subject, snippet, current in rows:
            new = classify(subject or "", snippet or "")
            stats[new] = stats.get(new, 0) + 1
            if new != current:
                c.execute(
                    text("UPDATE job_messages SET classification = :cls WHERE id = :id"),
                    {"cls": new, "id": id_},
                )
                changed += 1
        c.commit()
    log.info("reclassify: scanned %d messages, updated %d, distribution: %s",
             len(rows), changed, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
