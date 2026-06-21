"""
scripts/probe_icims.py
----------------------
Validate the ~10k iCIMS portal tokens (from the job-board-aggregator repo) by
hitting each portal's iframe search page once and keeping the ones that are live
and list jobs. Winners are written to data/discovered_icims.csv (seed schema)
for seed_companies.py to load.

Run from backend/:
    ../.venv/bin/python scripts/probe_icims.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(__file__)
OUT = os.path.join(HERE, "..", "data", "discovered_icims.csv")
SRC = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/HEAD/data/icims_companies.json"
LOCAL = "/tmp/icims_companies.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) JobControlCenter/1.0 (+personal-job-search)"}
PAGE = "https://careers-{t}.icims.com/jobs/search?ss=1&in_iframe=1&pr=0"
JOB_HREF = re.compile(r"/jobs/\d+/[^\"']+/job", re.I)

lock = threading.Lock()
winners: list = []
counter = {"done": 0, "live": 0}
TOTAL = 0


def name_from_token(tok: str) -> str:
    base = re.sub(r"-\d+$", "", tok).strip("-")
    return base.replace("-", " ").replace("_", " ").title() or tok


def probe(tok: str):
    n = 0
    try:
        r = requests.get(PAGE.format(t=tok), headers=UA, timeout=8)
        if r.status_code == 200:
            n = len(set(JOB_HREF.findall(r.text)))
    except Exception:
        n = 0
    with lock:
        counter["done"] += 1
        if n > 0:
            counter["live"] += 1
            winners.append((tok, n))
        if counter["done"] % 1000 == 0:
            print(f"  probed {counter['done']}/{TOTAL}, {counter['live']} live...", flush=True)


def main():
    global TOTAL
    if not os.path.exists(LOCAL):
        print("Downloading iCIMS token list...")
        urllib.request.urlretrieve(SRC, LOCAL)
    toks = json.load(open(LOCAL))
    toks = sorted({str(t).strip().lower().strip("-") for t in toks if t and 2 <= len(str(t)) <= 60})
    TOTAL = len(toks)
    print(f"Probing {TOTAL} iCIMS tokens...")

    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(probe, toks))

    rows = []
    for tok, n in sorted(winners, key=lambda x: -x[1]):
        rows.append([name_from_token(tok), tok, "icims", 40, "low", 1, f"icims-verified; {n}+ jobs"])
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"\nDone. {counter['live']} live iCIMS portals -> {len(rows)} written to {OUT}")


if __name__ == "__main__":
    main()
