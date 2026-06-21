"""
scripts/crawl_fast.py
---------------------
Parallel bulk crawl of EVERY active company. Fetches many companies concurrently
(network-bound) with a thread pool, then runs the pipeline + DB writes serially
in the main thread (SQLite has one writer). Use this to scan the full company
list quickly; the 24/7 live_watch then just keeps it fresh.

Each worker builds its OWN crawler instance (own requests.Session) so threads
don't share a session. Dedupe is done in-memory against existing hashes/urls.

Run from backend/:
    python scripts/crawl_fast.py            # only not-yet-crawled companies
    python scripts/crawl_fast.py --all      # re-crawl everything
    python scripts/crawl_fast.py --workers 24
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.crawlers.greenhouse import GreenhouseCrawler  # noqa: E402
from app.crawlers.lever import LeverCrawler  # noqa: E402
from app.crawlers.ashby import AshbyCrawler  # noqa: E402
from app.crawlers.smartrecruiters import SmartRecruitersCrawler  # noqa: E402
from app.crawlers.bamboohr import BambooHRCrawler  # noqa: E402
from app.crawlers.workable import WorkableCrawler  # noqa: E402
from app.crawlers.recruitee import RecruiteeCrawler  # noqa: E402
from app.crawlers.workday import WorkdayCrawler  # noqa: E402
from app.database import init_db, session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services import (  # noqa: E402
    cover_letter, filter_engine, pruner, resume_tailor, scoring_engine, sponsorship_engine,
)
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("crawl_fast")

# ats_type -> crawler class. A fresh instance is built per fetch (own session).
CRAWLER_CLASSES = {
    "greenhouse": GreenhouseCrawler, "lever": LeverCrawler, "ashby": AshbyCrawler,
    "smartrecruiters": SmartRecruitersCrawler, "bamboohr": BambooHRCrawler,
    "workable": WorkableCrawler, "recruitee": RecruiteeCrawler, "workday": WorkdayCrawler,
}


def fetch(company: Company):
    cls = CRAWLER_CLASSES.get((company.ats_type or "").strip().lower())
    if not cls:
        return company.id, []
    try:
        return company.id, cls().crawl(company)
    except Exception:  # noqa: BLE001
        return company.id, []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="re-crawl all (default: only uncrawled)")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    init_db()
    with session_scope() as session:
        companies = session.exec(select(Company).where(Company.is_active == True)).all()  # noqa: E712
        comp_by_id = {c.id: c for c in companies}
        todo = companies if args.all else [c for c in companies if c.last_checked_at is None]

        # In-memory dedupe sets (one-time load beats a SELECT per job).
        existing_hashes = {h for h in session.exec(select(Job.raw_data_hash)).all() if h}
        existing_urls = {u for u in session.exec(select(Job.job_url)).all() if u}
        stale_before = pruner.stale_cutoff()

        log.info("Fast crawl: %d companies, %d workers", len(todo), args.workers)
        added = done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(fetch, c) for c in todo]
            for fut in as_completed(futures):
                cid, jobs = fut.result()
                company = comp_by_id.get(cid)
                for job in jobs:
                    if pruner.is_stale(job, stale_before):
                        continue
                    if job.raw_data_hash and job.raw_data_hash in existing_hashes:
                        continue
                    if job.job_url and job.job_url in existing_urls:
                        continue
                    if job.raw_data_hash:
                        existing_hashes.add(job.raw_data_hash)
                    if job.job_url:
                        existing_urls.add(job.job_url)

                    result = filter_engine.evaluate(job)
                    if not result.passed:
                        job.status = "Rejected"
                        job.rejection_reason = result.reason
                    job.match_score, job.fit_reason = scoring_engine.score(job, company)
                    job.sponsorship_risk, job.risk_reason = sponsorship_engine.assess(job, company)
                    if job.sponsorship_risk == "reject" and job.status != "Rejected":
                        job.status = "Rejected"
                        job.rejection_reason = job.rejection_reason or job.risk_reason
                    if job.status != "Rejected":
                        job.status = ("New" if job.match_score >= settings.min_good_score
                                      and job.sponsorship_risk in ("low", "medium") else "Need Review")
                    # Materials for "New" (Best) jobs only — same gate as scheduler/rescore.
                    if job.status == "New" and job.match_score >= settings.materials_min_score:
                        try:
                            job.resume_notes = resume_tailor.generate(job)
                            job.cover_letter = cover_letter.generate(job, include_opt=False)
                        except Exception:  # noqa: BLE001
                            pass
                    session.add(job)
                    added += 1

                if company:
                    company.last_checked_at = datetime.utcnow()
                    session.add(company)
                done += 1
                if done % 200 == 0:
                    session.commit()
                    log.info("progress: %d/%d companies, %d jobs added", done, len(todo), added)

    log.info("FAST CRAWL DONE. companies=%d jobs_added=%d", done, added)


if __name__ == "__main__":
    main()
