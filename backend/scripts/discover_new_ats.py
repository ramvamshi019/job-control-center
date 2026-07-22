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
import json
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


def _paylocity(tok: str):
    # Paylocity boards are keyed by a company GUID, not a name-derived slug, so
    # this probe VALIDATES a candidate GUID/board URL harvested elsewhere rather
    # than guessing one. Non-GUID tokens bail out before any network call, which
    # is what keeps it free to leave in the cross-probe sweep below.
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", tok or "")
    if not m:
        return None
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{m.group(0)}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        # The board server-renders its whole posting list into window.pageData.
        m2 = re.search(r"window\.pageData\s*=\s*", r.text)
        if not m2:
            return None
        data, _ = json.JSONDecoder().raw_decode(r.text, m2.end())
        return len(data.get("Jobs", []) or [])
    except Exception:
        return None


def _ukg(tok: str):
    # UKG Pro boards need host + client code + board GUID, so the candidate token
    # must be a full board URL (or "host|code|guid"); bare slugs can't be probed.
    m = re.search(r"(?:https?://)?([a-z0-9.-]*ultipro\.com)/([A-Za-z0-9]+)/JobBoard/"
                  r"([0-9a-fA-F-]{36})", tok or "", re.I)
    if m:
        host, code, board = m.group(1).lower(), m.group(2), m.group(3)
    elif (tok or "").count("|") == 2:
        host, code, board = [p.strip() for p in tok.split("|")]
    else:
        return None
    url = f"https://{host}/{code}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    # The PascalCase `opportunitySearch` wrapper is mandatory: a flat body still
    # returns 200 but with totalCount 0.
    body = {"opportunitySearch": {"Top": 1, "Skip": 0, "QueryString": "",
                                  "OrderBy": [], "Filters": []}}
    try:
        r = requests.post(url, json=body, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json().get("totalCount", 0) or 0
    except Exception:
        return None


def _oracle_hcm(tok: str):
    # Oracle Cloud HCM ("ORC") boards live on a per-tenant Fusion pod hostname,
    # so the candidate token must carry that host (career-site URL or
    # "host|siteNumber"); the site number defaults to CX_1.
    s = (tok or "").strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        host, site = parts[0].lower(), (parts[1] if len(parts) > 1 else "") or "CX_1"
        if not host.endswith("oraclecloud.com"):
            return None
    else:
        m = re.search(r"(?:https?://)?([a-z0-9.-]+\.oraclecloud\.com)"
                      r"(?:/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+))?", s, re.I)
        if not m:
            return None
        host, site = m.group(1).lower(), (m.group(2) or "CX_1")
    url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&finder=findReqs;siteNumber={site},limit=1")
    try:
        r = requests.get(url, headers={**HEADERS, "Accept": "application/json"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        items = r.json().get("items") or []
        if not items:
            return None
        return items[0].get("TotalJobsCount", 0) or 0
    except Exception:
        return None


PROBES = {"smartrecruiters": _sr, "workable": _workable,
          "recruitee": _recruitee, "rippling": _rippling, "gem": _gem,
          "breezy": _breezy, "paylocity": _paylocity, "ukg": _ukg,
          "oracle_hcm": _oracle_hcm}

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
