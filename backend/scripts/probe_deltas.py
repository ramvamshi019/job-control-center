"""
scripts/probe_deltas.py
-----------------------
The job-board-aggregator token lists are much larger than what we originally
seeded. This probes ONLY the new tokens (those not already in the DB) for the
five ATS we already crawl, keeps the live boards with >0 jobs, and writes them
to data/discovered_deltas.csv (seed schema) for seed_companies.py.

Token lists are expected at /tmp/{ats}_companies.json (downloaded via curl).

Run from backend/:
    ../.venv/bin/python scripts/probe_deltas.py
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "data", "jobs.db")
OUT = os.path.join(HERE, "..", "data", "discovered_deltas.csv")
UA = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}
T = 8

ATS = ("greenhouse", "lever", "ashby", "bamboohr", "workday")

lock = threading.Lock()
winners: list = []
counter = {"done": 0, "live": 0}
TOTAL = 0


def name_from_token(tok: str) -> str:
    base = tok.split("|")[0]            # workday -> tenant
    base = re.sub(r"-\d+$", "", base).strip("-")
    return base.replace("-", " ").replace("_", " ").title() or tok


def count_greenhouse(t):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else None


def count_lever(t):
    r = requests.get(f"https://api.lever.co/v0/postings/{t}?mode=json", headers=UA, timeout=T)
    return len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else None


def count_ashby(t):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{t}", headers=UA, timeout=T)
    return len(r.json().get("jobs", [])) if r.status_code == 200 else None


def count_bamboohr(t):
    r = requests.get(f"https://{t}.bamboohr.com/careers/list", headers=UA, timeout=T)
    return len(r.json().get("result", [])) if r.status_code == 200 else None


def count_workday(t):
    parts = t.split("|")
    if len(parts) != 3:
        return None
    tenant, dc, site = parts
    url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    r = requests.post(url, headers={**UA, "Content-Type": "application/json"},
                      json={"limit": 1, "offset": 0, "searchText": ""}, timeout=T)
    return r.json().get("total", 0) if r.status_code == 200 else None


COUNTERS = {
    "greenhouse": count_greenhouse, "lever": count_lever, "ashby": count_ashby,
    "bamboohr": count_bamboohr, "workday": count_workday,
}


def probe(item):
    ats, tok = item
    try:
        n = COUNTERS[ats](tok)
    except Exception:
        n = None
    with lock:
        counter["done"] += 1
        if n and n > 0:
            counter["live"] += 1
            winners.append((ats, tok, n))
        if counter["done"] % 2000 == 0:
            print(f"  probed {counter['done']}/{TOTAL}, {counter['live']} live...", flush=True)


def main():
    global TOTAL
    con = sqlite3.connect(DB)
    items = []
    for ats in ATS:
        path = f"/tmp/{ats}_companies.json"
        if not os.path.exists(path):
            print(f"  (skip {ats}: {path} missing)")
            continue
        toks = {str(t).strip().lower() for t in json.load(open(path)) if t}
        indb = {(r[0] or "").strip().lower()
                for r in con.execute("select career_url from companies where ats_type=?", (ats,))}
        new = sorted(toks - indb)
        print(f"  {ats}: {len(new)} new tokens to probe")
        items += [(ats, t) for t in new]
    con.close()
    TOTAL = len(items)
    print(f"Probing {TOTAL} new tokens across {len(ATS)} ATS...")

    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(probe, items))

    rows = []
    for ats, tok, n in sorted(winners, key=lambda x: -x[2]):
        rows.append([name_from_token(tok), tok, ats, 40, "low", 1, f"delta-verified; {n} jobs"])
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)

    by = {}
    for r in rows:
        by[r[2]] = by.get(r[2], 0) + 1
    print(f"\nDone. {counter['live']} live boards -> {len(rows)} new companies -> {OUT}")
    print("By ATS:", by)


if __name__ == "__main__":
    main()
