"""
scripts/sniff_ats.py
--------------------
Take a list of company careers URLs, fingerprint each one's ATS, and write a
seed CSV that seed_companies.py can load.

Complements discover_new_ats.py:
    discover_new_ats.py    -> probes NAME TOKENS against ATS APIs (guessing)
    sniff_ats.py           -> fingerprints EXPLICIT URLs (definitive)

The sniffer detects the widest set of ATSes — including phenom, successfactors,
taleo, eightfold — that the token probes CAN'T guess because they need full
hostnames, not slugs.

Input CSV format (data/careers_urls_seed.csv), one row per company:
    name,url
    Snowflake,https://careers.snowflake.com
    JPMorgan Chase,https://careers.jpmorgan.com
    Oracle,https://careers.oracle.com

Output: data/discovered_sniff.csv in seed_companies.py's schema. Duplicates
against the live DB are skipped (checked by name + career_url).

Usage:
    ../.venv/bin/python scripts/sniff_ats.py                      # uses default input
    ../.venv/bin/python scripts/sniff_ats.py --input <path>       # custom input
    ../.venv/bin/python scripts/sniff_ats.py --workers 20         # concurrency
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.utils.ats_sniff import sniff  # noqa: E402

HERE = os.path.dirname(__file__)
DEFAULT_IN = os.path.join(HERE, "..", "data", "careers_urls_seed.csv")
DEFAULT_OUT = os.path.join(HERE, "..", "data", "discovered_sniff.csv")
DB_CANDIDATES = [
    "/app/backend/data/db/jobs.db",
    os.path.join(HERE, "..", "data", "jobs.db"),
]


def find_db() -> str:
    for c in DB_CANDIDATES:
        if os.path.exists(c):
            return c
    raise SystemExit("could not locate jobs.db")


def load_existing() -> tuple[set[str], set[str]]:
    """Return (names, career_urls) already present so we don't re-seed."""
    con = sqlite3.connect(find_db())
    names = {r[0].strip().lower() for r in con.execute("SELECT name FROM companies") if r[0]}
    urls = {r[0].strip().lower() for r in con.execute("SELECT career_url FROM companies") if r[0]}
    con.close()
    return names, urls


def read_input(path: str) -> list[tuple[str, str]]:
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            url = (row.get("url") or row.get("career_url") or "").strip()
            if name and url:
                rows.append((name, url))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_IN)
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"input not found: {args.input}\n"
                         "Create it with columns: name,url")

    todo = read_input(args.input)
    if args.limit:
        todo = todo[: args.limit]
    print(f"input: {args.input} ({len(todo)} rows)")

    seen_names, seen_urls = load_existing()

    hits: list[tuple[str, str, str]] = []   # (name, ats_type, career_url)
    misses: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []  # already-in-db

    def _work(name_url):
        name, url = name_url
        ats, seed = sniff(url)
        return name, url, ats, seed

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (name, url, ats, seed) in enumerate(ex.map(_work, todo), 1):
            if ats and seed:
                if name.lower() in seen_names and seed.lower() in seen_urls:
                    skipped.append((name, ats, seed))
                else:
                    hits.append((name, ats, seed))
            else:
                misses.append((name, url))
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}  hits={len(hits)} miss={len(misses)} skip={len(skipped)}",
                      flush=True)

    print(f"\ndone: {len(hits)} new hits, {len(skipped)} already-in-db, {len(misses)} unrecognized")
    if hits:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "career_url", "ats_type", "h1b_history_score",
                        "priority", "is_active", "notes"])
            for name, ats, seed in hits:
                w.writerow([name, seed, ats, 40, "medium", 1, "auto-sniffed via sniff_ats.py"])
        print(f"wrote {args.output}")
        print("Load with: python scripts/seed_companies.py "
              f"--csv {args.output}")

    # Breakdown by ATS
    by_ats: dict[str, int] = {}
    for _, ats, _ in hits:
        by_ats[ats] = by_ats.get(ats, 0) + 1
    if by_ats:
        print("\nby ATS:")
        for ats, n in sorted(by_ats.items(), key=lambda x: -x[1]):
            print(f"  {ats:20s} {n}")

    if misses:
        print("\nfirst 15 misses (no ATS marker found):")
        for name, url in misses[:15]:
            print(f"  {name!r} -> {url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
