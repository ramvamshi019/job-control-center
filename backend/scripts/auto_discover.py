"""
scripts/auto_discover.py
------------------------
Grow the company list automatically. Without this the crawler pulls new JOBS
24/7 but never finds new COMPANIES: the roster was seeded once (21,606 rows on
2026-06-17) and then sat frozen for over a month, because every discovery script
in this repo had to be run by hand and nothing ever called them.

Strategy: most companies run their board on a token derived from their own name,
and many run boards on MORE than one ATS. So for every company already known, we
generate candidate tokens from its name and probe the public, keyless list APIs
of the token-based platforms. A board that answers with >0 live postings is real
and gets seeded.

Only ATSes with a public token API can be discovered this way. Paylocity, UKG
and Oracle HCM are deliberately excluded: their boards are keyed by GUIDs and
per-tenant hostnames that cannot be derived from a company name, so probing them
would be pure waste.

    python scripts/auto_discover.py --dry-run       # report, write nothing
    python scripts/auto_discover.py                 # one pass
    python scripts/auto_discover.py --loop --every-hours 168   # weekly forever

Safe to run alongside the live crawler: probing is pure HTTP, and the only DB
writes are INSERTs of brand-new companies, committed in small batches so the
single SQLite writer is never held for long.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("auto_discover")

HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}
TIMEOUT = 12

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))


# --- Probes: each returns a live posting count, or None if it isn't a board. ---
# Note on SmartRecruiters: a bogus token and a real-but-empty board BOTH return
# HTTP 200 with totalFound 0, so >0 postings is the only trustworthy signal.
def _smartrecruiters(tok: str):
    r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=1",
                    timeout=TIMEOUT)
    return (r.json() or {}).get("totalFound", 0) if r.status_code == 200 else None


def _greenhouse(tok: str):
    r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs", timeout=TIMEOUT)
    return len(r.json().get("jobs", []) or []) if r.status_code == 200 else None


def _lever(tok: str):
    r = SESSION.get(f"https://api.lever.co/v0/postings/{tok}?mode=json", timeout=TIMEOUT)
    d = r.json() if r.status_code == 200 else None
    return len(d) if isinstance(d, list) else None


def _ashby(tok: str):
    r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{tok}", timeout=TIMEOUT)
    return len(r.json().get("jobs", []) or []) if r.status_code == 200 else None


def _workable(tok: str):
    r = SESSION.get(f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true",
                    timeout=TIMEOUT)
    return len(r.json().get("jobs", []) or []) if r.status_code == 200 else None


def _recruitee(tok: str):
    r = SESSION.get(f"https://{tok}.recruitee.com/api/offers/", timeout=TIMEOUT)
    return len(r.json().get("offers", []) or []) if r.status_code == 200 else None


PROBES = {
    "smartrecruiters": _smartrecruiters,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "recruitee": _recruitee,
}


def candidates(name: str) -> list[str]:
    """Token forms these ATSes actually use. Lowercase only — every probed API
    is case-insensitive, so probing both cases would just double the traffic."""
    base = re.sub(r"[^A-Za-z0-9 ]+", "", name or "").strip()
    if not base:
        return []
    squashed = base.replace(" ", "").lower()
    hyphen = base.replace(" ", "-").lower()
    out = [squashed] + ([hyphen] if hyphen != squashed else [])
    return [t for t in out if len(t) >= 3]


def one_pass(dry_run: bool = False, workers: int = 16, limit: int = 0) -> dict:
    with session_scope() as s:
        companies = s.exec(select(Company)).all()
        # (ats, token) pairs we already have — never re-probe or re-insert these.
        known = {(c.ats_type or "", (c.career_url or "").strip().lower()) for c in companies}
        names = [(c.name, c.h1b_history_score or 0, c.priority or "medium") for c in companies]

    if limit:
        names = names[:limit]
    # Sponsors first: if a pass is cut short, the highest-value boards are done.
    names.sort(key=lambda t: -t[1])
    log.info("auto-discovery: probing %d companies across %d platforms",
             len(names), len(PROBES))

    def probe_company(item):
        name, score, prio = item
        hits = []
        for tok in candidates(name):
            for ats, fn in PROBES.items():
                if (ats, tok) in known:
                    continue
                try:
                    n = fn(tok)
                except Exception:
                    continue
                if n and n > 0:
                    hits.append((name, tok, ats, n, score, prio))
        return hits

    found, done = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for hits in ex.map(probe_company, names):
            done += 1
            if done % 2000 == 0:
                log.info("  ...%d/%d probed, %d boards found", done, len(names), len(found))
            found.extend(hits)

    # Dedupe within this run: the same (ats, token) can be reached from two
    # differently-named rows for the same employer.
    uniq = {}
    for name, tok, ats, n, score, prio in found:
        uniq.setdefault((ats, tok), (name, tok, ats, n, score, prio))

    summary = {"probed": len(names), "boards": len(uniq),
               "postings": sum(v[3] for v in uniq.values()), "added": 0}
    if dry_run:
        log.info("DRY RUN: %d new boards, %d live postings (nothing written)",
                 summary["boards"], summary["postings"])
        for v in sorted(uniq.values(), key=lambda v: -v[3])[:15]:
            log.info("   %-28s %-16s %-14s %5d postings", v[0][:28], v[1][:16], v[2], v[3])
        return summary

    batch = []
    with session_scope() as s:
        for name, tok, ats, n, score, prio in uniq.values():
            batch.append(Company(
                name=name, career_url=tok, ats_type=ats,
                h1b_history_score=score, priority=prio, is_active=True,
                notes=f"auto-discovered {datetime.utcnow():%Y-%m-%d}; {n} live postings"))
            if len(batch) >= 200:
                s.add_all(batch); s.commit(); summary["added"] += len(batch); batch = []
        if batch:
            s.add_all(batch); s.commit(); summary["added"] += len(batch)
    log.info("auto-discovery: added %d companies (%d live postings behind them)",
             summary["added"], summary["postings"])
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="run forever on an interval")
    ap.add_argument("--every-hours", type=float, default=168.0, help="loop interval (default weekly)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N companies")
    args = ap.parse_args()

    while True:
        try:
            one_pass(dry_run=args.dry_run, workers=args.workers, limit=args.limit)
        except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the loop
            log.warning("discovery pass failed: %s", exc)
        if not args.loop:
            return 0
        log.info("next discovery pass in %.1fh", args.every_hours)
        time.sleep(max(600, args.every_hours * 3600))


if __name__ == "__main__":
    sys.exit(main())
