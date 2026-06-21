"""
scripts/harvest_tokens.py
-------------------------
Grow coverage of the ATS crawlers we ALREADY run by harvesting company board
tokens out of our richest feeds:
  - the new-grad / internship GitHub lists (apply URLs)
  - the current Hacker News "Who is hiring?" thread (links in comments)

Many of those apply-links point at Greenhouse/Lever/Ashby/Rippling/Gem/etc.
boards we don't yet crawl. We extract the token, drop ones we already have, and
write a seed CSV (data/harvested_tokens.csv) for seed_companies.py. Seeding a
board pulls that company's ENTIRE careers page, not just the one listed role.

Re-runnable: only NEW tokens are written. Run from backend/:
    ../.venv/bin/python scripts/harvest_tokens.py
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys

import requests

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "data", "jobs.db")
OUT = os.path.join(HERE, "..", "data", "harvested_tokens.csv")
H = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}
TIMEOUT = 30

# ATS -> regex capturing the board token from a URL (run over lowercased text).
PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_.-]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9_.-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)",
    "rippling": r"ats\.rippling\.com/([a-z0-9_.-]+)",
    "gem": r"jobs\.gem\.com/([a-z0-9_.-]+)",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/([a-z0-9_.-]+)",
    "bamboohr": r"([a-z0-9_-]+)\.bamboohr\.com",
    "breezy": r"([a-z0-9_-]+)\.breezy\.hr",
    "workable": r"apply\.workable\.com/([a-z0-9_-]+)",
}
JUNK = {"embed", "job", "jobs", "careers", "career", "www", "apply", "for", "o", "j", "search"}


def feed_text() -> str:
    """Concatenate apply-URLs from new-grad lists + links from the live HN thread."""
    blobs = []
    for u in (
        "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ):
        try:
            data = requests.get(u, headers=H, timeout=TIMEOUT).json()
            blobs += [j.get("url", "") for j in data if j.get("active")]
        except Exception as exc:  # noqa: BLE001
            print(f"  new_grad fetch failed: {exc}")
    try:
        s = requests.get("https://hn.algolia.com/api/v1/search?query=Ask%20HN%20Who%20is%20hiring&tags=story",
                         headers=H, timeout=TIMEOUT).json()
        sid = s["hits"][0]["objectID"]
        item = requests.get(f"https://hn.algolia.com/api/v1/items/{sid}", headers=H, timeout=TIMEOUT).json()
        blobs += [c.get("text", "") for c in (item.get("children") or []) if c.get("text")]
        print(f"  HN thread: {sid} ({len(item.get('children') or [])} comments)")
    except Exception as exc:  # noqa: BLE001
        print(f"  HN fetch failed: {exc}")
    return "\n".join(blobs).lower()


def existing_tokens() -> dict:
    con = sqlite3.connect(DB)
    out = {}
    for ats in PATTERNS:
        out[ats] = {(r[0] or "").lower().strip() for r in
                    con.execute("select career_url from companies where ats_type=?", (ats,))}
    con.close()
    return out


def name_from_token(tok: str) -> str:
    return re.sub(r"-\d+$", "", tok).replace("-", " ").replace("_", " ").title() or tok


def main() -> None:
    print("Harvesting tokens from new_grad + HN feeds...")
    text = feed_text()
    have = existing_tokens()
    rows, by = [], {}
    for ats, rx in PATTERNS.items():
        found = {m.group(1).strip("-.") for m in re.finditer(rx, text)}
        new = {t for t in found if t and t not in JUNK and 2 <= len(t) <= 60 and t not in have[ats]}
        by[ats] = len(new)
        for t in sorted(new):
            rows.append([name_from_token(t), t, ats, 40, "medium", 1, "harvested from new_grad/HN feed"])

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "career_url", "ats_type", "h1b_history_score", "priority", "is_active", "notes"])
        w.writerows(rows)
    print(f"\nNew boards harvested: {len(rows)} -> {OUT}")
    print("By ATS:", {k: v for k, v in by.items() if v})


if __name__ == "__main__":
    main()
