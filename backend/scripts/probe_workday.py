"""
scripts/probe_workday.py
------------------------
Validate the Workday tenant list (/tmp/workday_companies.json, entries shaped
"tenant|dc|site") and keep only boards that are live and have >=1 US job.
Writes winners to data/discovered_workday.csv (seed schema; career_url holds the
"tenant|dc|site" string).

Run from backend/:
    python scripts/probe_workday.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.filter_engine import is_us_location  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "discovered_workday.csv")
EXISTING = os.path.join(os.path.dirname(__file__), "..", "data", "companies_seed.csv")
HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search)",
           "Accept": "application/json", "Content-Type": "application/json"}

lock = threading.Lock()
winners = []
counters = {"done": 0, "live": 0, "us": 0}


def probe(entry):
    try:
        tenant, dc, site = entry.split("|")[:3]
    except ValueError:
        with lock:
            counters["done"] += 1
        return
    url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    us_jobs = 0
    try:
        r = requests.post(url, headers=HEADERS,
                          json={"limit": 20, "offset": 0, "searchText": "", "appliedFacets": {}},
                          timeout=10)
        if r.status_code == 200:
            d = r.json()
            jp = d.get("jobPostings", []) or []
            if jp:
                with lock:
                    counters["live"] += 1
                for j in jp:
                    if is_us_location(j.get("locationsText") or ""):
                        us_jobs += 1
    except Exception:
        pass
    with lock:
        counters["done"] += 1
        if us_jobs > 0:
            counters["us"] += 1
            winners.append((entry, tenant, us_jobs))
        if counters["done"] % 1000 == 0:
            print(f"  probed {counters['done']}, live={counters['live']}, with-US={counters['us']}...",
                  flush=True)


def main():
    entries = [str(e).strip() for e in json.load(open("/tmp/workday_companies.json")) if e and "|" in str(e)]
    print(f"Probing {len(entries)} Workday boards (keeping those with >=1 US job)...")

    existing = set()
    if os.path.exists(EXISTING):
        for row in csv.DictReader(open(EXISTING)):
            existing.add((row["ats_type"].strip().lower(), row["career_url"].strip().lower()))

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(probe, entries))

    seen, rows = set(), []
    for entry, tenant, n in sorted(winners, key=lambda x: -x[2]):
        if ("workday", entry.lower()) in existing or entry in seen:
            continue
        seen.add(entry)
        name = tenant.replace("-", " ").replace("_", " ").title()
        rows.append([name, entry, "workday", 50, "low", 1, f"workday; {n} US jobs"])

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"DONE. live={counters['live']} with-US={counters['us']}; {len(rows)} new written to {OUT}")


if __name__ == "__main__":
    main()
