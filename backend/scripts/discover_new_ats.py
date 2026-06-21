"""
scripts/discover_new_ats.py
---------------------------
Seed the three ATS platforms whose crawlers already exist but had zero companies:
SmartRecruiters, Workable, Recruitee.

Strategy: many companies run boards on more than one ATS under the same token.
So we take every existing company token in the DB (greenhouse/lever/ashby/
bamboohr) plus any curated lists, and probe each token against the three new
ATS public APIs. Boards that are live and return >0 jobs are written to
data/discovered_new_ats.csv (seed schema) for seed_companies.py to load.

Run from backend/:
    ../.venv/bin/python scripts/discover_new_ats.py
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "data", "jobs.db")
OUT = os.path.join(HERE, "..", "data", "discovered_new_ats.csv")
SEED = os.path.join(HERE, "..", "data", "companies_seed.csv")
HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}
TIMEOUT = 8

# Each probe returns the live job count for that token, or None if not a board.
def _sr(tok: str):
    url = f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=10"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json().get("totalFound", 0) or len(r.json().get("content", []) or [])
    except Exception:
        return None


def _workable(tok: str):
    url = f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return len(r.json().get("jobs", []) or [])
    except Exception:
        return None


def _recruitee(tok: str):
    url = f"https://{tok}.recruitee.com/api/offers/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return len(r.json().get("offers", []) or [])
    except Exception:
        return None


def _rippling(tok: str):
    # Bare-token match only: the winner is seeded with career_url=tok, so the
    # working slug must equal the token (boards using a "-job-board" suffix are
    # seeded directly, not via this cross-probe).
    url = f"https://ats.rippling.com/api/v2/board/{tok}/jobs?page=0&pageSize=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json().get("totalItems", 0)
    except Exception:
        return None


def _gem(tok: str):
    # Gem boards are keyed by vanity slug (== a friendly token). Validate via the
    # GraphQL batch endpoint: a real board has a non-null jobBoardExternal.
    q = ("query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) "
         "{ jobPostings { id } } jobBoardExternal(vanityUrlPath: $boardId) { id teamDisplayName } }")
    payload = [{"operationName": "JobBoardList", "variables": {"boardId": tok}, "query": q}]
    try:
        r = requests.post("https://jobs.gem.com/api/public/graphql/batch", json=payload,
                          headers={**HEADERS, "batch": "true", "Content-Type": "application/json"},
                          timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        body = r.json()
        if not isinstance(body, list) or not body:
            return None
        data = body[0].get("data") or {}
        if not (data.get("jobBoardExternal") or {}).get("teamDisplayName"):
            return None  # not a real board
        return len(((data.get("oatsExternalJobPostings") or {}).get("jobPostings")) or [])
    except Exception:
        return None


def _breezy(tok: str):
    url = f"https://{tok}.breezy.hr/json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        return len(data) if isinstance(data, list) else None
    except Exception:
        return None


PROBES = {"smartrecruiters": _sr, "workable": _workable,
          "recruitee": _recruitee, "rippling": _rippling, "gem": _gem,
          "breezy": _breezy}

lock = threading.Lock()
winners: list = []
counter = {"done": 0, "live": 0}
TOTAL = 0


def name_from_token(tok: str) -> str:
    base = re.sub(r"-\d+$", "", tok)
    return base.replace("-", " ").replace("_", " ").title() or tok


def probe(item):
    ats, tok = item
    n = PROBES[ats](tok)
    with lock:
        counter["done"] += 1
        if n and n > 0:
            counter["live"] += 1
            winners.append((ats, tok, n))
        if counter["done"] % 2000 == 0:
            print(f"  probed {counter['done']}/{TOTAL}, {counter['live']} live boards...", flush=True)


def candidate_tokens() -> list:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select distinct lower(career_url) from companies "
        "where ats_type in ('greenhouse','lever','ashby','bamboohr')"
    ).fetchall()
    con.close()
    toks = set()
    for (u,) in rows:
        u = (u or "").strip()
        # keep only clean bare tokens (skip workday-style pipe/url junk)
        if u and "|" not in u and "/" not in u and "." not in u and 2 <= len(u) <= 60:
            toks.add(u)
    return sorted(toks)


def main():
    global TOTAL
    toks = candidate_tokens()
    print(f"{len(toks)} unique candidate tokens; probing each against 3 ATS...")
    items = [(ats, t) for t in toks for ats in PROBES]
    TOTAL = len(items)

    existing = set()
    if os.path.exists(SEED):
        for row in csv.DictReader(open(SEED)):
            existing.add((row["ats_type"].strip().lower(), row["career_url"].strip().lower()))

    with ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(probe, items))

    seen, rows = set(), []
    for ats, tok, n in sorted(winners, key=lambda x: -x[2]):
        key = (ats, tok)
        if key in seen or key in existing:
            continue
        seen.add(key)
        rows.append([name_from_token(tok), tok, ats, 40, "low", 1, f"cross-probe verified; {n} jobs"])

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)

    by = {}
    for r in rows:
        by[r[2]] = by.get(r[2], 0) + 1
    print(f"\nDone. {counter['live']} live boards -> {len(rows)} new companies written to {OUT}")
    print("By ATS:", by)


if __name__ == "__main__":
    main()
