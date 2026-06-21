"""
scripts/backfill_bamboohr_dates.py
----------------------------------
One-time fix: existing BambooHR jobs were stamped posted_at = first-seen (the
list API has no date). This fetches each job's REAL datePosted from its detail
endpoint and updates the DB. After this, the 10-day retention pruner will drop
the genuinely-old ones on its next cycle (run scripts/prune_old_jobs.py to do it
now), leaving accurate, fresh BambooHR dates.

Pause the live crawler first (so DB writes don't lock):
    launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.jobcontrolcenter.livewatch.plist
Run from backend/:
    ../.venv/bin/python scripts/backfill_bamboohr_dates.py
"""
from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

DB = os.path.join(os.path.dirname(__file__), "..", "data", "jobs.db")
UA = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}

lock = threading.Lock()
updates: list = []      # (date_str, job_id)
counter = {"done": 0, "got": 0}
TOTAL = 0


def fetch_date(row):
    job_id, job_url = row
    date_str = None
    try:
        r = requests.get(f"{job_url}/detail", headers=UA, timeout=10)
        if r.status_code == 200:
            dp = (r.json().get("result", {}).get("jobOpening", {}) or {}).get("datePosted")
            if dp:  # "YYYY-MM-DD" -> match the DB datetime format
                date_str = f"{dp} 00:00:00.000000"
    except Exception:
        pass
    with lock:
        counter["done"] += 1
        if date_str:
            counter["got"] += 1
            updates.append((date_str, job_id))
        if counter["done"] % 1000 == 0:
            print(f"  {counter['done']}/{TOTAL} fetched, {counter['got']} real dates...", flush=True)


def main():
    global TOTAL
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    rows = con.execute(
        "select id, job_url from jobs where source='bamboohr' and job_url is not null and job_url!=''"
    ).fetchall()
    TOTAL = len(rows)
    print(f"Backfilling real posted dates for {TOTAL} BambooHR jobs...")

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(fetch_date, rows))

    print(f"\nWriting {len(updates)} updated dates...")
    con.executemany("update jobs set posted_at=?, updated_at=datetime('now') where id=?", updates)
    con.commit()

    # Report how many are now older than the 10-day retention window.
    stale = con.execute(
        "select count(*) from jobs where source='bamboohr' and posted_at < datetime('now','-10 days') "
        "and status not in ('Approved','Applied','Follow-up')"
    ).fetchone()[0]
    print(f"Done. {counter['got']}/{TOTAL} got real dates.")
    print(f"{stale} BambooHR jobs are now >10 days old and will be pruned next cycle.")
    con.close()


if __name__ == "__main__":
    main()
