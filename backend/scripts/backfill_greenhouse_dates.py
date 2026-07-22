"""
scripts/backfill_greenhouse_dates.py
------------------------------------
One-off repair for jobs stored while the greenhouse crawler read `updated_at`
(last EDIT) instead of `first_published` (real publish date) as posted_at.

Recruiters re-touch old reqs constantly, so long-dead listings looked brand new:
75,585 of 75,598 stored greenhouse jobs claimed a posted_at inside the 10-day
retention window, which meant the pruner never expired any of them and the
dashboard's freshness ranking was fiction.

The crawler is fixed going forward, but dedupe.is_duplicate() SKIPS rows that
already exist rather than updating them, so stored jobs keep the bad date until
this script corrects them.

How it works: greenhouse's board list endpoint returns first_published for every
job on a board, so this costs ONE request per board (~3.4k), not per job (~76k).

    python scripts/backfill_greenhouse_dates.py            # apply
    python scripts/backfill_greenhouse_dates.py --dry-run  # report only

After applying, the normal pruner deletes whatever is now genuinely older than
settings.prune_days. Approved/Applied/Follow-up jobs are protected by the pruner
and are never lost.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

DB = "/app/backend/data/db/jobs.db"
LIST_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
URL_RE = re.compile(r"greenhouse\.io/([^/?]+)/jobs/(\d+)")


def parse_iso(s):
    """Greenhouse returns tz-aware ISO; store naive UTC to match the column."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fetch_board(token):
    """-> {greenhouse_job_id: first_published_str}. Empty dict on any failure so
    one dead board never aborts the run."""
    for attempt in range(2):
        try:
            r = requests.get(LIST_API.format(token=token), timeout=25)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return {str(j.get("id")): j.get("first_published")
                    for j in (r.json().get("jobs") or [])}
        except Exception:
            if attempt:
                return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=30000")

    rows = con.execute(
        "select id, job_url, posted_at from jobs where source='greenhouse' and job_url != ''"
    ).fetchall()
    by_token = defaultdict(list)   # token -> [(our_id, gh_id, old_posted)]
    for our_id, url, posted in rows:
        m = URL_RE.search(url or "")
        if m:
            by_token[m.group(1)].append((our_id, m.group(2), posted))
    print(f"greenhouse jobs: {len(rows)}  across {len(by_token)} boards", flush=True)

    updates, missing, unchanged = [], 0, 0
    done = 0
    with cf.ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(fetch_board, t): t for t in by_token}
        for fut in cf.as_completed(futs):
            token = futs[fut]
            pub = fut.result()
            done += 1
            if done % 250 == 0:
                print(f"  ...{done}/{len(by_token)} boards", flush=True)
            for our_id, gh_id, old in by_token[token]:
                real = parse_iso(pub.get(gh_id))
                if real is None:
                    missing += 1
                    continue
                if old and real.isoformat()[:19] == str(old)[:19]:
                    unchanged += 1
                    continue
                updates.append((real.isoformat(sep=" "), our_id))

    print(f"\ncorrections found : {len(updates)}")
    print(f"already correct   : {unchanged}")
    print(f"not on board / 404: {missing}  (left untouched)")

    cutoff = datetime.utcnow() - timedelta(days=10)
    would_prune = sum(1 for iso, _ in updates if datetime.fromisoformat(iso) < cutoff)
    print(f"\nnewly older than the 10-day window: {would_prune}"
          f"  ({100*would_prune/max(len(updates),1):.0f}% of corrections)")
    print("   -> the normal pruner removes these next cycle "
          "(Approved/Applied/Follow-up are protected)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    # Chunked commits so the live crawler is never blocked behind one huge txn.
    for i in range(0, len(updates), 2000):
        con.executemany("update jobs set posted_at=? where id=?", updates[i:i + 2000])
        con.commit()
    print(f"\napplied {len(updates)} corrections.")


if __name__ == "__main__":
    sys.exit(main())
