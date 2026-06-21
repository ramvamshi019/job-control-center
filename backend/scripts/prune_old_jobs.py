"""
scripts/prune_old_jobs.py
-------------------------
Delete jobs older than PRUNE_DAYS (default 10) to keep the database light.
Jobs you've Approved / Applied / Follow-up'd are always kept.

Run from backend/:
    python scripts/prune_old_jobs.py
    python scripts/prune_old_jobs.py --days 7
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import init_db, session_scope  # noqa: E402
from app.services import pruner  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("prune")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="override retention window")
    args = ap.parse_args()

    init_db()
    with session_scope() as session:
        n = pruner.prune_old_jobs(session, days=args.days)
    log.info("Prune complete. Removed %d jobs.", n)


if __name__ == "__main__":
    main()
