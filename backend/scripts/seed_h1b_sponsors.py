"""Seed NEW companies from the USCIS H-1B Employer Data Hub.

auto_discover.py only expands boards for companies we ALREADY know (it probes
name-derived tokens of existing rows). This script brings in brand-new *names*
— every employer in the USCIS H-1B approvals CSV that we don't already track —
and probes each against the token-based ATSes. A confirmed H-1B sponsor with a
live board is the single highest-value company we can add for an F-1/OPT search,
so hits are inserted at **high priority** (they crawl often; the low-value tail
crawls less, so total crawler load stays balanced).

Reuses the existing pipeline wholesale:
  - enrich_h1b.norm / score_for   -> same employer-name normalization + scoring
  - auto_discover.PROBES / candidates -> same keyless board probes + token forms

Source CSV: the same /tmp/h1b_merged.csv that enrich_h1b.py consumes (USCIS
H-1B Employer Data Hub, columns include employer_name / initial_approval /
continuing_approval). Download it first if it isn't present.

Usage:
    python scripts/seed_h1b_sponsors.py --dry-run              # report only
    python scripts/seed_h1b_sponsors.py --min-approvals 5      # write hits
    python scripts/seed_h1b_sponsors.py --limit 500 --dry-run  # quick test
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

# Reuse the exact probing + scoring the rest of the system already uses, so a
# board this script finds is identical to one auto_discover would have found,
# and its H-1B score lines up with enrich_h1b's.
from auto_discover import PROBES, candidates  # noqa: E402
from enrich_h1b import norm, score_for  # noqa: E402

log = get_logger("seed_h1b_sponsors")

SRC = "/tmp/h1b_merged.csv"


def load_sponsor_approvals() -> dict[str, tuple[str, int]]:
    """normalized employer name -> (display_name, total_approvals). Mirrors
    enrich_h1b's aggregation so scores match."""
    if not os.path.exists(SRC):
        raise SystemExit(
            f"Missing {SRC} -- download the USCIS H-1B Employer Data Hub CSV "
            f"first (same file enrich_h1b.py uses).")
    approvals: dict[str, int] = {}
    display: dict[str, str] = {}
    rows = 0
    with open(SRC, newline="", encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            rows += 1
            raw = r.get("employer_name") or ""
            key = norm(raw)
            if not key:
                continue
            a = 0
            for col in ("initial_approval", "continuing_approval"):
                try:
                    a += int(float(r.get(col) or 0))
                except (TypeError, ValueError):
                    pass
            approvals[key] = approvals.get(key, 0) + a
            # Keep the first (usually cleanest) display spelling we saw.
            display.setdefault(key, raw.strip())
    log.info("read %d USCIS rows -> %d distinct sponsor employers", rows, len(approvals))
    return {k: (display[k], approvals[k]) for k in approvals}


def one_pass(min_approvals: int, dry_run: bool, workers: int, limit: int) -> dict:
    sponsors = load_sponsor_approvals()

    with session_scope() as s:
        companies = s.exec(select(Company)).all()
        known_names = {norm(c.name) for c in companies}
        known_boards = {(c.ats_type or "", (c.career_url or "").strip().lower())
                        for c in companies}

    # New = a sponsor we meet the bar for AND don't already track by name.
    new = [(disp, appr) for key, (disp, appr) in sponsors.items()
           if appr >= min_approvals and key not in known_names]
    new.sort(key=lambda t: -t[1])  # most-sponsoring employers first
    if limit:
        new = new[:limit]
    log.info("%d new sponsor employers (>=%d approvals, not already tracked); probing "
             "%d platforms", len(new), min_approvals, len(PROBES))

    def probe(item):
        name, appr = item
        hits = []
        for tok in candidates(name):
            for ats, fn in PROBES.items():
                if (ats, tok) in known_boards:
                    continue
                try:
                    n = fn(tok)
                except Exception:
                    continue
                if n and n > 0:
                    hits.append((name, tok, ats, n, appr))
        return hits

    found, done = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for hits in ex.map(probe, new):
            done += 1
            if done % 2000 == 0:
                log.info("  ...%d/%d probed, %d boards found", done, len(new), len(found))
            found.extend(hits)

    # One employer's token can be reached from two near-duplicate names; keep one.
    uniq: dict[tuple[str, str], tuple] = {}
    for name, tok, ats, n, appr in found:
        uniq.setdefault((ats, tok), (name, tok, ats, n, appr))

    summary = {"new_sponsors": len(new), "boards": len(uniq),
               "postings": sum(v[3] for v in uniq.values()), "added": 0}

    if dry_run:
        log.info("DRY RUN: %d sponsor boards, %d live postings (nothing written)",
                 summary["boards"], summary["postings"])
        for v in sorted(uniq.values(), key=lambda v: -v[3])[:20]:
            log.info("   %-30s %-16s %-14s %5d postings  (%d approvals)",
                     v[0][:30], v[1][:16], v[2], v[3], v[4])
        return summary

    batch = []
    with session_scope() as s:
        for name, tok, ats, n, appr in uniq.values():
            batch.append(Company(
                name=name, career_url=tok, ats_type=ats,
                h1b_history_score=score_for(appr),
                priority="high",  # confirmed sponsor -> crawl often
                is_active=True,
                notes=f"h1b-seeded {datetime.utcnow():%Y-%m-%d}; "
                      f"{appr} USCIS approvals; {n} live postings"))
            if len(batch) >= 200:
                s.add_all(batch); s.commit(); summary["added"] += len(batch); batch = []
        if batch:
            s.add_all(batch); s.commit(); summary["added"] += len(batch)
    log.info("added %d sponsor companies (%d live postings behind them)",
             summary["added"], summary["postings"])
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--min-approvals", type=int, default=5,
                    help="minimum USCIS approvals to treat a name as a real sponsor")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="cap employers probed (testing)")
    a = ap.parse_args()
    one_pass(a.min_approvals, a.dry_run, a.workers, a.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
