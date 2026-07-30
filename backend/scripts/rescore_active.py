"""
scripts/rescore_active.py
-------------------------
One-shot rescore of ONLY the currently-active pool (New, Need Review)
so a scoring change lands in Ram's queue *now* without waiting for
next-crawl on all companies. Small pool (~9k rows) so this completes
in a couple minutes, unlike rescore_all.py which covers all 763k.

Trigger: run after any scoring_engine.py or filter_engine.py change.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import filter_engine, scoring_engine, sponsorship_engine  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("rescore_active")


def run() -> dict:
    counts = {"scanned": 0, "unchanged": 0, "kept_new": 0,
              "moved_review": 0, "moved_rejected": 0}
    with session_scope() as session:
        jobs = session.exec(
            select(Job).where(Job.status.in_(["New", "Need Review"]))
        ).all()
        cids = {j.company_id for j in jobs if j.company_id}
        companies = {c.id: c for c in session.exec(
            select(Company).where(Company.id.in_(cids))).all()} if cids else {}

        mappings = []
        for job in jobs:
            counts["scanned"] += 1
            company = companies.get(job.company_id)

            # Re-evaluate through the current filter + scoring engines.
            status, rejection_reason = "New", ""
            result = filter_engine.evaluate(job)
            if not result.passed:
                status, rejection_reason = "Rejected", result.reason
            match_score, fit_reason = scoring_engine.score(job, company)
            spr, risk_reason = sponsorship_engine.assess(job, company)
            if spr == "reject" and status != "Rejected":
                status, rejection_reason = "Rejected", (rejection_reason or risk_reason)
            if status != "Rejected":
                if match_score >= settings.min_good_score and spr in ("low", "medium"):
                    status = "New"
                    counts["kept_new"] += 1
                else:
                    status = "Need Review"
                    counts["moved_review"] += 1
            else:
                counts["moved_rejected"] += 1

            if (status == job.status and match_score == job.match_score):
                counts["unchanged"] += 1
                continue
            mappings.append({
                "id": job.id, "status": status,
                "rejection_reason": rejection_reason,
                "match_score": match_score, "fit_reason": fit_reason,
                "sponsorship_risk": spr, "risk_reason": risk_reason,
            })

        if mappings:
            session.bulk_update_mappings(Job, mappings)
    log.info("rescore_active: %s", counts)
    return counts


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
