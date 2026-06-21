"""
scripts/enrich_h1b.py
---------------------
Stamp REAL H-1B sponsorship scores onto companies using the USCIS H-1B Employer
Data Hub (per-employer approval counts). Until now ~every company sat at
h1b_history_score=40 ("unknown" -> -30 scoring penalty); this flips proven
sponsors to a high score so their jobs surface as Best instead of Need Review.

Source CSV expected at /tmp/h1b_merged.csv with columns:
    fiscal_year, employer_name, ..., initial_approval, continuing_approval, ...

Run from backend/:
    ../.venv/bin/python scripts/enrich_h1b.py
Then rescore:
    ../.venv/bin/python scripts/rescore_all.py
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
from collections import defaultdict

HERE = os.path.dirname(__file__)
DB = os.path.join(HERE, "..", "data", "jobs.db")
SRC = "/tmp/h1b_merged.csv"

# Legal-entity noise to strip so "Stripe" matches "STRIPE, INC."
SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "pllc", "pc", "na",
}


def norm(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)                 # punctuation -> space
    words = s.split()
    # Strip legal suffixes only from the END, so words like "co"/"company"/"na"
    # that legitimately appear mid-name aren't deleted (avoids false negatives).
    while words and words[-1] in SUFFIXES:
        words.pop()
    return "".join(words)                              # collapse spacing too


def score_for(approvals: int) -> int:
    if approvals >= 100:
        return 95
    if approvals >= 20:
        return 88
    if approvals >= 5:
        return 78
    if approvals >= 1:
        return 65
    return 45  # matched the employer but only denials / zero approvals


def main():
    if not os.path.exists(SRC):
        raise SystemExit(f"Missing {SRC} (download the USCIS merged CSV first)")

    # 1) Aggregate approvals per normalized employer name.
    approvals = defaultdict(int)
    rows = 0
    with open(SRC, newline="", encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            rows += 1
            key = norm(r.get("employer_name"))
            if not key or len(key) < 3:
                continue
            ia = int(r.get("initial_approval") or 0)
            ca = int(r.get("continuing_approval") or 0)
            approvals[key] += ia + ca
    print(f"Read {rows} USCIS rows -> {len(approvals)} distinct employers")

    # 2) Match companies and update scores.
    con = sqlite3.connect(DB)
    cur = con.cursor()
    # Enrich ALL companies (rescore_all loads them all regardless of is_active).
    companies = cur.execute("select id, name, h1b_history_score from companies").fetchall()
    matched = updated = big = 0
    for cid, name, old in companies:
        key = norm(name)
        appr = approvals.get(key)
        if appr is None:
            continue  # leave unmatched at their existing (unknown=40) score
        matched += 1
        new = score_for(appr)
        if appr >= 20:
            big += 1
        if new != old:
            cur.execute("update companies set h1b_history_score=?, updated_at=datetime('now') where id=?", (new, cid))
            updated += 1
    con.commit()

    total = len(companies)
    pct = (matched * 100 // total) if total else 0
    print(f"\nCompanies: {total}")
    print(f"  matched to a known H-1B employer : {matched}  ({pct}%)")
    print(f"  scores updated                   : {updated}")
    print(f"  confirmed strong sponsors (>=20) : {big}")
    print("\nNew h1b_history_score distribution:")
    for r in cur.execute("select case when h1b_history_score>=88 then '88-95 (strong)' "
                         "when h1b_history_score>=65 then '65-78 (sponsor)' "
                         "when h1b_history_score>=50 then '50-64' "
                         "when h1b_history_score=45 then '45 (matched,0 appr)' "
                         "else '40 (unknown)' end b, count(*) from companies where is_active=1 group by b order by b desc"):
        print(f"   {r[0]:22} {r[1]}")
    con.close()


if __name__ == "__main__":
    main()
