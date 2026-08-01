"""
scripts/migrate_canonical_key.py
--------------------------------
Add jobs.canonical_key column, index it, and backfill from existing rows.
Safe to re-run (checks for the column and NULL rows first).

canonical_key is the cross-source dedupe key: the same real posting seen via
Greenhouse + Simplify + HN Hiring all collapse to one row after this ships.
See app/utils/text.canonical_job_key for the normalization rules.

Also reports what would collapse if we deduped now — no data is deleted; the
new key just becomes the third dedup layer so FUTURE inserts skip cross-source
duplicates. Existing dupes remain but can be cleaned by an operator query if
desired (never automatically; some may carry irreplaceable applied/rejected
state).

    ../.venv/bin/python scripts/migrate_canonical_key.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.utils.text import canonical_job_key  # noqa: E402


def _find_db() -> str:
    for candidate in (
        "/app/backend/data/db/jobs.db",
        os.path.join(os.path.dirname(__file__), "..", "data", "jobs.db"),
        os.path.join(os.path.dirname(__file__), "..", "data", "db", "jobs.db"),
    ):
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("could not locate jobs.db")


def main() -> int:
    db = _find_db()
    print(f"db: {db}")
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "canonical_key" in cols:
        print("canonical_key column already present")
    else:
        con.execute("ALTER TABLE jobs ADD COLUMN canonical_key VARCHAR DEFAULT ''")
        con.commit()
        print("added column jobs.canonical_key")

    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_canonical_key ON jobs(canonical_key)")
    con.commit()
    print("index idx_jobs_canonical_key ready")

    n_null = con.execute(
        "SELECT count(*) FROM jobs WHERE canonical_key IS NULL OR canonical_key = ''"
    ).fetchone()[0]
    print(f"rows needing backfill: {n_null}", flush=True)
    if n_null:
        # Cursor-over-ids pattern (NOT `WHERE canonical_key = ''` — canonical_job_key
        # legitimately returns '' for rows without enough signal, so a sentinel-based
        # loop never terminates when the update writes '' back).
        BATCH = 5000
        max_id = con.execute("SELECT COALESCE(max(id),0) FROM jobs").fetchone()[0]
        last_id = 0
        done = 0
        while last_id <= max_id:
            rows = con.execute(
                "SELECT id, company_name, title, location FROM jobs "
                "WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, BATCH),
            ).fetchall()
            if not rows:
                break
            updates = [(canonical_job_key(cn or "", t or "", loc or ""), jid) for jid, cn, t, loc in rows]
            con.executemany("UPDATE jobs SET canonical_key = ? WHERE id = ?", updates)
            con.commit()
            done += len(rows)
            last_id = rows[-1][0]
            print(f"  backfilled {done}/{n_null} (id<={last_id})", flush=True)

    # Report cross-source duplicate groups so the impact of the new key is visible.
    dup_groups = con.execute(
        "SELECT canonical_key, count(*) c, group_concat(DISTINCT source) srcs "
        "FROM jobs WHERE canonical_key != '' "
        "GROUP BY canonical_key HAVING c > 1 ORDER BY c DESC LIMIT 15"
    ).fetchall()
    dup_row_count = con.execute(
        "SELECT count(*) - count(DISTINCT canonical_key) "
        "FROM jobs WHERE canonical_key != ''"
    ).fetchone()[0]
    print(f"\n{dup_row_count} row(s) are cross-source duplicates of an already-stored posting.")
    print("Top 15 dup groups (would collapse to 1 each going forward):")
    for key, count, srcs in dup_groups:
        print(f"  x{count} ({srcs})  key={key[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
