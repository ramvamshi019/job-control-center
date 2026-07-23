"""
scripts/rescore_all.py
----------------------
Re-apply the WHOLE evaluation pipeline (hard filters -> score -> sponsorship ->
routing -> resume notes + cover letter) to jobs ALREADY in the database.

Why this exists: a re-crawl skips jobs it has seen (dedupe), so changing your
skills (.env), the US-only filter, or the scoring rules does NOT affect jobs that
are already stored. Run this once after changing any of those.

It only touches pipeline-managed jobs (New / Need Review / Rejected / Archived).
Jobs you've manually actioned (Approved / Applied / Follow-up) are left alone so
your decisions are never overwritten.

Run from the backend/ folder:
    python scripts/rescore_all.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import (  # noqa: E402
    cover_letter,
    filter_engine,
    resume_tailor,
    scoring_engine,
    sponsorship_engine,
)
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("rescore_all")

# Statuses the user owns — never re-route these. "Archived" is included so a job
# you manually dismissed stays dismissed instead of being re-scored back to New.
PROTECTED = {"Approved", "Applied", "Follow-up", "Archived"}


def main() -> None:
    init_db()
    counts = {"total": 0, "rescored": 0, "protected": 0, "rejected": 0,
              "best": 0, "review": 0}

    # Stream jobs in id-range chunks instead of loading the whole table. The DB
    # is >500k rows; a single select(Job).all() holds every row (plus its
    # description) in RAM at once and OOM-kills this on the 2 GB box. Each chunk
    # runs in its OWN short session that also loads ONLY the companies its jobs
    # reference, so peak memory is bounded by CHUNK regardless of table size —
    # and short sessions stay safe to run beside the live crawler.
    CHUNK = 4000
    last_id = 0
    while True:
        with session_scope() as session:
            jobs = session.exec(
                select(Job).where(Job.id > last_id).order_by(Job.id).limit(CHUNK)
            ).all()
            if not jobs:
                break
            last_id = jobs[-1].id
            cids = {j.company_id for j in jobs if j.company_id}
            companies = (
                {c.id: c for c in session.exec(
                    select(Company).where(Company.id.in_(cids))).all()}
                if cids else {}
            )

            mappings: list[dict] = []
            for job in jobs:
                counts["total"] += 1
                if job.status in PROTECTED:
                    counts["protected"] += 1
                    continue

                company = companies.get(job.company_id)
                status, rejection_reason = "New", ""

                result = filter_engine.evaluate(job)
                if not result.passed:
                    status, rejection_reason = "Rejected", result.reason

                match_score, fit_reason = scoring_engine.score(job, company)
                sponsorship_risk, risk_reason = sponsorship_engine.assess(job, company)
                if sponsorship_risk == "reject" and status != "Rejected":
                    status, rejection_reason = "Rejected", (rejection_reason or risk_reason)

                if status != "Rejected":
                    if match_score >= settings.min_good_score and sponsorship_risk in ("low", "medium"):
                        status = "New"
                        counts["best"] += 1
                    else:
                        status = "Need Review"
                        counts["review"] += 1
                else:
                    counts["rejected"] += 1

                resume_notes, cover = job.resume_notes, job.cover_letter
                if status == "New" and match_score >= settings.materials_min_score:
                    try:
                        resume_notes = resume_tailor.generate(job)
                        cover = cover_letter.generate(job, include_opt=False)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("material gen failed for '%s': %s", job.title, exc)

                mappings.append({
                    "id": job.id, "status": status, "rejection_reason": rejection_reason,
                    "match_score": match_score, "fit_reason": fit_reason,
                    "sponsorship_risk": sponsorship_risk, "risk_reason": risk_reason,
                    "resume_notes": resume_notes, "cover_letter": cover,
                })

            # One executemany UPDATE per chunk (keyed on id) — far faster than a
            # statement per row, and the chunk commits together on scope exit.
            if mappings:
                session.bulk_update_mappings(Job, mappings)
                counts["rescored"] += len(mappings)

        log.info("...rescored through id=%d (total=%d, best=%d, review=%d, rejected=%d)",
                 last_id, counts["total"], counts["best"], counts["review"], counts["rejected"])

    log.info(
        "Re-score done. total=%(total)d rescored=%(rescored)d protected=%(protected)d "
        "| best=%(best)d review=%(review)d rejected=%(rejected)d", counts,
    )


if __name__ == "__main__":
    main()
