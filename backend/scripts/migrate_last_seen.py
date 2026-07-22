"""
scripts/migrate_last_seen.py
----------------------------
Add jobs.last_seen_at and backfill it. Run ONCE after deploying the ghost-job
change; safe to re-run (checks for the column first).

SQLModel's create_all() only creates missing TABLES, never missing COLUMNS, so
an existing database needs this ALTER TABLE explicitly.

Backfill uses discovered_at, i.e. "last confirmed alive when we first saw it".
That is the honest floor: we have no record of re-sightings before this change
shipped. It means every pre-existing job starts its ghost clock from when it was
discovered, so genuinely dead old postings expire on the first pass while
anything still on a board gets its timestamp refreshed on the next crawl.

    python scripts/migrate_last_seen.py
"""
from __future__ import annotations

import sqlite3
import sys

DB = "/app/backend/data/db/jobs.db"


def main() -> int:
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "last_seen_at" in cols:
        print("last_seen_at already present — nothing to do.")
    else:
        con.execute("ALTER TABLE jobs ADD COLUMN last_seen_at DATETIME")
        con.commit()
        print("added column jobs.last_seen_at")

    n = con.execute("select count(*) from jobs where last_seen_at is null").fetchone()[0]
    print(f"rows needing backfill: {n}")
    if n:
        con.execute("update jobs set last_seen_at = discovered_at where last_seen_at is null")
        con.commit()
        print("backfilled from discovered_at")

    con.execute("create index if not exists idx_jobs_last_seen on jobs(last_seen_at)")
    con.commit()
    print("index idx_jobs_last_seen ready")

    # What the first ghost-prune will remove, so it's never a surprise.
    for d in (21, 30):
        k = con.execute(
            "select count(*) from jobs where last_seen_at < datetime('now', ?) "
            "and status not in ('Approved','Applied','Follow-up')", (f"-{d} day",)
        ).fetchone()[0]
        print(f"  would prune at ghost_days={d}: {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
