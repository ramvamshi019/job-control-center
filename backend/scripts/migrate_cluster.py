"""
scripts/migrate_cluster.py
--------------------------
Add jobs.cluster column, index it, backfill via services.cluster.classify().
Safe to re-run. Cursor-over-ids pattern so the loop terminates even when
classify() returns '' for edge cases.

    ../.venv/bin/python scripts/migrate_cluster.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.cluster import classify  # noqa: E402


def _find_db() -> str:
    for c in (
        "/app/backend/data/db/jobs.db",
        os.path.join(os.path.dirname(__file__), "..", "data", "jobs.db"),
    ):
        if os.path.exists(c):
            return c
    raise SystemExit("could not locate jobs.db")


def main() -> int:
    db = _find_db()
    print(f"db: {db}")
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "cluster" in cols:
        print("cluster column already present")
    else:
        con.execute("ALTER TABLE jobs ADD COLUMN cluster VARCHAR DEFAULT ''")
        con.commit()
        print("added column jobs.cluster")

    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster)")
    con.commit()
    print("index idx_jobs_cluster ready")

    n_null = con.execute(
        "SELECT count(*) FROM jobs WHERE cluster IS NULL OR cluster = ''"
    ).fetchone()[0]
    print(f"rows needing backfill: {n_null}", flush=True)
    if n_null:
        BATCH = 5000
        max_id = con.execute("SELECT COALESCE(max(id),0) FROM jobs").fetchone()[0]
        last_id = 0
        done = 0
        while last_id <= max_id:
            rows = con.execute(
                "SELECT id, title, description FROM jobs WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, BATCH),
            ).fetchall()
            if not rows:
                break
            updates = [(classify(t or "", d or ""), jid) for jid, t, d in rows]
            con.executemany("UPDATE jobs SET cluster = ? WHERE id = ?", updates)
            con.commit()
            done += len(rows)
            last_id = rows[-1][0]
            print(f"  backfilled {done}/{n_null} (id<={last_id})", flush=True)

    # Distribution snapshot so the impact is visible.
    print("\ncluster distribution:")
    for c, n in con.execute(
        "SELECT cluster, count(*) FROM jobs GROUP BY cluster ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {c or '(none)':20s} {n:>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
