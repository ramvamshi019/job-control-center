"""
scripts/probe_token_lists.py
----------------------------
Validate large lists of known Greenhouse/Lever/Ashby board tokens (downloaded to
/tmp/{ats}_companies.json) by hitting each public ATS API once, keeping only the
boards that are live and return jobs. Writes winners to
data/discovered_bulk.csv (seed schema) for merging.

Run from backend/:
    python scripts/probe_token_lists.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "discovered_bulk.csv")
EXISTING = os.path.join(os.path.dirname(__file__), "..", "data", "companies_seed.csv")

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
}
HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search)"}

lock = threading.Lock()
winners = []
counters = {"done": 0, "live": 0}


def name_from_token(tok: str) -> str:
    base = re.sub(r"-\d+$", "", tok)              # drop trailing -2, -3 dupes
    return base.replace("-", " ").replace("_", " ").title() or tok


def count_jobs(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        jobs = d.get("jobs", d) if isinstance(d, dict) else d
        return len(jobs) if isinstance(jobs, list) else None
    except Exception:
        return None


def probe(item):
    ats, tok = item
    n = count_jobs(URLS[ats].format(t=tok))
    with lock:
        counters["done"] += 1
        if n and n > 0:
            counters["live"] += 1
            winners.append((ats, tok, n))
        if counters["done"] % 1000 == 0:
            print(f"  probed {counters['done']} tokens, {counters['live']} live...", flush=True)


def main():
    items = []
    for ats in ("greenhouse", "lever", "ashby"):
        path = f"/tmp/{ats}_companies.json"
        toks = json.load(open(path))
        items += [(ats, str(t).strip().lower()) for t in toks if t and 2 <= len(str(t)) <= 60]
    print(f"Probing {len(items)} known tokens across Greenhouse/Lever/Ashby...")

    existing = set()
    if os.path.exists(EXISTING):
        for row in csv.DictReader(open(EXISTING)):
            existing.add((row["ats_type"].strip().lower(), row["career_url"].strip().lower()))

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(probe, items))

    seen, rows = set(), []
    for ats, tok, n in sorted(winners, key=lambda x: -x[2]):
        key = (ats, tok)
        if key in seen or key in existing:
            continue
        seen.add(key)
        rows.append([name_from_token(tok), tok, ats, 40, "low", 1, f"bulk-verified; {n} jobs"])

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score",
                    "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"DONE. {counters['live']} live boards; {len(rows)} new (not already seeded) "
          f"written to {OUT}")


if __name__ == "__main__":
    main()
