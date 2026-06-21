"""
scripts/discover_companies.py
-----------------------------
Mass-discover Greenhouse / Lever / Ashby company boards by probing public ATS
APIs with candidate tokens derived from the Y Combinator company dataset
(~6k startups, many of which sponsor and hire engineers in the US).

Writes winners to data/discovered_companies.csv (same schema as the seed file),
which you then merge into companies_seed.csv and seed.

Run from backend/:
    python scripts/discover_companies.py
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

YC_JSON = "/tmp/yc_all.json"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "discovered_companies.csv")
EXISTING = os.path.join(os.path.dirname(__file__), "..", "data", "companies_seed.csv")

GH = "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"
LV = "https://api.lever.co/v0/postings/{t}?mode=json"
AS = "https://api.ashbyhq.com/posting-api/job-board/{t}"
HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search)"}

lock = threading.Lock()
winners = []
done = [0]


def tokenize(name: str, slug: str):
    cands = set()
    if slug:
        cands.add(slug.strip().lower())
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if n:
        cands.add(n)
    # also a dashed variant of the name
    d = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if d:
        cands.add(d)
    return [c for c in cands if 2 <= len(c) <= 40]


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


def probe(company):
    name = company.get("name", "")
    for tok in tokenize(name, company.get("slug", "")):
        for ats, tmpl in (("greenhouse", GH), ("lever", LV), ("ashby", AS)):
            n = count_jobs(tmpl.format(t=tok))
            if n and n > 0:
                with lock:
                    winners.append((ats, tok, name, n))
                return  # one hit per company is enough
    with lock:
        done[0] += 1
        if done[0] % 250 == 0:
            print(f"  probed {done[0]} companies, {len(winners)} boards found...", flush=True)


def main():
    companies = json.load(open(YC_JSON))
    # Skip dead companies to cut noise; keep everything else.
    companies = [c for c in companies if (c.get("status") or "").lower() != "dead"]
    print(f"Probing {len(companies)} YC companies across Greenhouse/Lever/Ashby...")

    existing_tokens = set()
    if os.path.exists(EXISTING):
        for row in csv.DictReader(open(EXISTING)):
            existing_tokens.add((row["ats_type"].strip().lower(), row["career_url"].strip().lower()))

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(probe, companies))

    # Dedupe + drop ones already seeded.
    seen, rows = set(), []
    for ats, tok, name, n in sorted(winners, key=lambda x: -x[3]):
        key = (ats, tok)
        if key in seen or key in existing_tokens:
            continue
        seen.add(key)
        rows.append([name, tok, ats, 40, "low", 1, f"YC-discovered; {n} jobs"])

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score",
                    "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"DONE. {len(rows)} new boards written to {OUT}")


if __name__ == "__main__":
    main()
