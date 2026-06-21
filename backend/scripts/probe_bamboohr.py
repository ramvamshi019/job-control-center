"""
scripts/probe_bamboohr.py
-------------------------
Validate the BambooHR token list (/tmp/bamboohr_companies.json) and keep only
boards that have at least one US-based job. Writes winners to
data/discovered_bamboohr.csv (seed schema).

Run from backend/:
    python scripts/probe_bamboohr.py
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.filter_engine import is_us_location  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "discovered_bamboohr.csv")
EXISTING = os.path.join(os.path.dirname(__file__), "..", "data", "companies_seed.csv")
API = "https://{t}.bamboohr.com/careers/list"
HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search)", "Accept": "application/json"}

lock = threading.Lock()
winners = []
counters = {"done": 0, "live": 0, "us": 0}


def name_from_token(tok: str) -> str:
    return re.sub(r"-\d+$", "", tok).replace("-", " ").replace("_", " ").title() or tok


def loc_str(raw):
    loc = raw.get("location") or {}
    ats = raw.get("atsLocation") or {}
    parts = [loc.get("city") or ats.get("city"), loc.get("state") or ats.get("state"), ats.get("country")]
    s = ", ".join(p for p in parts if p)
    return s or ("Remote" if raw.get("isRemote") else "")


def probe(tok):
    us_jobs = 0
    try:
        r = requests.get(API.format(t=tok), headers=HEADERS, timeout=8)
        if r.status_code == 200:
            res = r.json().get("result", []) or []
            if res:
                with lock:
                    counters["live"] += 1
                for j in res:
                    if is_us_location(loc_str(j)):
                        us_jobs += 1
    except Exception:
        pass
    with lock:
        counters["done"] += 1
        if us_jobs > 0:
            counters["us"] += 1
            winners.append((tok, us_jobs))
        if counters["done"] % 1000 == 0:
            print(f"  probed {counters['done']}, live={counters['live']}, with-US-jobs={counters['us']}...",
                  flush=True)


def main():
    toks = [str(t).strip().lower() for t in json.load(open("/tmp/bamboohr_companies.json"))
            if t and 2 <= len(str(t)) <= 60]
    print(f"Probing {len(toks)} BambooHR boards (keeping those with >=1 US job)...")

    existing = set()
    if os.path.exists(EXISTING):
        for row in csv.DictReader(open(EXISTING)):
            existing.add((row["ats_type"].strip().lower(), row["career_url"].strip().lower()))

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(probe, toks))

    rows = []
    for tok, n in sorted(winners, key=lambda x: -x[1]):
        if ("bamboohr", tok) in existing:
            continue
        rows.append([name_from_token(tok), tok, "bamboohr", 40, "low", 1, f"bamboohr; {n} US jobs"])

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"DONE. live={counters['live']} with-US-jobs={counters['us']}; {len(rows)} new written to {OUT}")


if __name__ == "__main__":
    main()
