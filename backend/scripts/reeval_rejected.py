"""
scripts/reeval_rejected.py
--------------------------
Re-run the CURRENT filter/scoring pipeline over jobs that were rejected by an
older, stricter version of the rules, and promote whatever now passes.

Needed because filters run once, at crawl time: `dedupe.is_duplicate()` skips
rows that already exist, so loosening a rule has no effect on the ~420k jobs
already sitting in the DB marked Rejected.

Written for two changes made 2026-07-22:
  * "senior"/"sr." dropped from TITLE_BLOCK (~19,100 jobs) — the YEARS_BLOCK
    patterns already reject anything wanting 5+ years, which is the real limit.
  * blank/unknown locations now fall back to the description (~71k jobs).

    python scripts/reeval_rejected.py --dry-run          # report only
    python scripts/reeval_rejected.py --reason senior    # only that bucket
    python scripts/reeval_rejected.py                    # apply to all Rejected

Never touches Approved / Applied / Follow-up — your decisions stand.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

sys.path.insert(0, "/app/backend")

from sqlmodel import select

from app.config import settings
from app.database import session_scope
from app.models.company import Company
from app.models.job import Job
from app.services import filter_engine, scoring_engine, sponsorship_engine

PROTECTED = ("Approved", "Applied", "Follow-up")
CHUNK = 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reason", default=None,
                    help="only re-check jobs whose rejection_reason contains this")
    ap.add_argument("--include-visible", action="store_true",
                    help="ALSO re-check New/Need Review jobs and demote any that now "
                         "fail — needed after ADDING a rule (e.g. the company "
                         "blocklist), since rejected-only scanning can't remove "
                         "something already on screen")
    args = ap.parse_args()

    with session_scope() as s:
        statuses = ["Rejected"] + (["New", "Need Review"] if args.include_visible else [])
        stmt = select(Job).where(Job.status.in_(statuses))
        if args.reason:
            stmt = stmt.where(Job.rejection_reason.contains(args.reason))
        jobs = s.exec(stmt).all()
        print(f"re-evaluating {len(jobs)} rejected jobs", flush=True)

        companies = {c.id: c for c in s.exec(select(Company)).all()}
        promoted, still, routes = 0, Counter(), Counter()

        demoted = 0
        for i, job in enumerate(jobs, 1):
            result = filter_engine.evaluate(job)
            if not result.passed:
                still[result.reason.split(":")[0]] += 1
                # A visible job that now fails must come OFF the dashboard.
                if job.status != "Rejected":
                    job.status = "Rejected"
                    job.rejection_reason = result.reason
                    demoted += 1
                    if not args.dry_run:
                        s.add(job)
                continue

            company = companies.get(job.company_id)
            job.match_score, job.fit_reason = scoring_engine.score(job, company)
            job.sponsorship_risk, job.risk_reason = sponsorship_engine.assess(job, company)
            if job.sponsorship_risk == "reject":
                still["sponsorship risk"] += 1
                continue

            # Same routing the crawler uses, so a re-checked job is
            # indistinguishable from a freshly crawled one.
            if (job.match_score >= settings.min_good_score
                    and job.sponsorship_risk in ("low", "medium")):
                job.status = "New"
            else:
                job.status = "Need Review"
            job.rejection_reason = ""
            routes[job.status] += 1
            promoted += 1
            if not args.dry_run:
                s.add(job)

            # Commit in chunks: SQLite is a single writer and the live crawler
            # needs it back between batches.
            if not args.dry_run and promoted % CHUNK == 0:
                s.commit()
                print(f"  ...{i}/{len(jobs)} scanned, {promoted} promoted", flush=True)

        print(f"\npromoted    : {promoted}")
        print(f"demoted     : {demoted}  (were visible, now fail current rules)")
        for k, n in routes.most_common():
            print(f"   -> {k:<12} {n}")
        print(f"still rejected: {sum(still.values())}")
        for k, n in still.most_common(8):
            print(f"   {n:>7}  {k}")

        if args.dry_run:
            print("\n--dry-run: nothing written.")
            s.rollback()
        else:
            s.commit()
            print("\napplied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
