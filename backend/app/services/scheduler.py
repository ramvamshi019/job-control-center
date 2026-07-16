"""
services/scheduler.py
---------------------
Two responsibilities:

1) PRIORITY SCHEDULING — decide which companies are "due" for a re-check, so we
   don't hammer every site every 20 minutes (see PART 2 of the README).
        high   -> every 30 min
        medium -> every 3 hours
        low    -> every 24 hours
        skip   -> never (no-sponsor / dead companies)

2) THE CRAWL PIPELINE — `process_company` and `run_crawl` tie everything
   together: crawl -> dedupe -> hard filter -> score -> sponsorship -> tailor
   -> cover letter -> save. Scripts call run_crawl().
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List

from sqlmodel import Session, select

from app.config import settings
from app.crawlers.registry import get_crawler_for
from app.models.company import Company
from app.models.job import Job
from app.services import (
    cover_letter,
    dedupe,
    filter_engine,
    pruner,
    resume_tailor,
    scoring_engine,
    sponsorship_engine,
)
from app.utils.logging import get_logger

log = get_logger("scheduler")

# How stale a company can be before we re-check it, by priority.
# "high" is the fast watchlist (confirmed H-1B sponsors) — re-checked every
# 20 min so a new posting shows up within ~20 min, not a day.
PRIORITY_INTERVALS = {
    # high was 15 min, but 3,881 high companies * 4/hr = ~15k scans/hr demand vs
    # ~2.5k/hr capacity on the 2GB box -> the high lane saturated the batch and
    # starved the 18k low companies (weeks stale). 3h keeps good-fit companies
    # fresh (8x/day) while leaving capacity to sweep every company within ~24h.
    "high": timedelta(hours=3),      # confirmed-sponsor / good-fit watchlist
    "medium": timedelta(hours=6),
    "low": timedelta(hours=24),
    "skip": None,  # never
}

# Lower = serviced first. Ensures watchlist re-checks beat the long-tail backlog.
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, "skip": 3}


def priority_rank(company: Company) -> int:
    return PRIORITY_RANK.get((company.priority or "medium").lower(), 1)


def is_due(company: Company) -> bool:
    """True if this company should be crawled now."""
    interval = PRIORITY_INTERVALS.get((company.priority or "medium").lower(), timedelta(hours=3))
    if interval is None:
        return False
    if company.last_checked_at is None:
        return True
    return datetime.utcnow() - company.last_checked_at >= interval


def due_companies(session: Session) -> List[Company]:
    companies = session.exec(select(Company).where(Company.is_active == True)).all()  # noqa: E712
    return [c for c in companies if is_due(c)]


def fetch_company_jobs(company: Company) -> List[Job]:
    """NETWORK-ONLY: crawl one company and return raw Job objects.

    Touches no DB session, so it is safe to call from worker threads. It only
    reads already-loaded company columns (name/career_url/ats_type) and builds
    detached Job objects — no lazy DB access.
    """
    crawler = get_crawler_for(company)
    if not crawler:
        return []
    return crawler.crawl(company)


def persist_company_jobs(session: Session, company: Company, jobs: List[Job]) -> dict:
    """DB-BOUND: run the pipeline on already-fetched jobs and save. SQLite is a
    single writer, so this must run on the main thread only."""
    summary = {"company": company.name, "found": len(jobs), "new": 0, "rejected": 0, "saved": 0}
    crawler = get_crawler_for(company)
    stale_before = pruner.stale_cutoff()

    # Dedupe FIRST (cheap, indexed) so expensive work only touches genuinely new
    # jobs. Then fill in REAL posted dates for those new jobs — some sources
    # (BambooHR) don't expose a date in their list API, so the crawler fetches it
    # from a per-job detail endpoint. Done concurrently, new-jobs-only, to stay fast.
    new_jobs = [j for j in jobs if not dedupe.is_duplicate(session, j)]
    enrich = getattr(crawler, "enrich_posted_date", None)
    if enrich and new_jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(enrich, new_jobs))

    for job in new_jobs:
        # Skip postings older than the retention window — now that the date is REAL,
        # this correctly prunes stale BambooHR jobs that used to look fresh.
        if pruner.is_stale(job, stale_before):
            continue
        summary["new"] += 1

        # 2) Hard filters.
        result = filter_engine.evaluate(job)
        if not result.passed:
            job.status = "Rejected"
            job.rejection_reason = result.reason
            summary["rejected"] += 1

        # 3) Score (still scored so you can audit borderline ones).
        job.match_score, job.fit_reason = scoring_engine.score(job, company)

        # 4) Sponsorship risk.
        job.sponsorship_risk, job.risk_reason = sponsorship_engine.assess(job, company)
        if job.sponsorship_risk == "reject" and job.status != "Rejected":
            job.status = "Rejected"
            job.rejection_reason = job.rejection_reason or job.risk_reason

        # 5) Route surviving jobs.
        if job.status not in ("Rejected",):
            if job.match_score >= settings.min_good_score and job.sponsorship_risk in ("low", "medium"):
                job.status = "New"          # shows in "Today's Best Jobs"
            else:
                job.status = "Need Review"

        # 6) Generate materials for "New" (Best) jobs only — the ones you act on.
        #    Need-Review jobs get materials on demand via the Regenerate button;
        #    skipping them here keeps each crawl cycle fast for freshness.
        if job.status == "New" and job.match_score >= settings.materials_min_score:
            try:
                job.resume_notes = resume_tailor.generate(job)
                job.cover_letter = cover_letter.generate(job, include_opt=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("material generation failed for '%s': %s", job.title, exc)

        session.add(job)
        summary["saved"] += 1

    company.last_checked_at = datetime.utcnow()
    company.updated_at = datetime.utcnow()
    session.add(company)
    session.commit()
    return summary


def process_company(session: Session, company: Company) -> dict:
    """Crawl one company end-to-end (fetch + persist). Serial convenience used
    by scripts; the live watcher uses run_crawl_parallel."""
    return persist_company_jobs(session, company, fetch_company_jobs(company))


def run_crawl(session: Session, companies: List[Company]) -> List[dict]:
    summaries = []
    for company in companies:
        log.info("Crawling %s (%s)…", company.name, company.ats_type)
        summaries.append(process_company(session, company))
    return summaries


def run_crawl_parallel(session: Session, companies: List[Company], workers: int = 8) -> List[dict]:
    """Fetch companies CONCURRENTLY and persist each one the moment its fetch
    finishes — fetch (network) and persist (DB) overlap instead of running in
    two blocking phases. SQLite stays single-writer (persist runs only on this
    thread). Roughly `workers`x faster than serial with identical DB behavior."""
    if not companies:
        return []

    summaries = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(fetch_company_jobs, c): c for c in companies}
        for fut in as_completed(futures):
            company = futures[fut]
            try:
                jobs = fut.result()
            except Exception as exc:  # noqa: BLE001 - one bad company can't kill the batch
                log.warning("fetch failed for %s: %s", company.name, exc)
                jobs = []
            summaries.append(persist_company_jobs(session, company, jobs))
    return summaries
