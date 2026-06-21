"""
crawlers/smartrecruiters.py
---------------------------
SmartRecruiters public Posting API (no key needed):
    list:   https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset=N
            -> {"content": [...], "totalFound": N, "offset": .., "limit": ..}
    detail: https://api.smartrecruiters.com/v1/companies/{token}/postings/{id}
            -> full posting incl. jobAd.sections (the real description),
               postingUrl/applyUrl, and the released date.

`token` is the company identifier (e.g. "Visa"). Accepts a bare token or a
jobs.smartrecruiters.com/{token} URL.

WHY THIS CRAWLER USED TO PULL ~1 JOB TOTAL (the bug this file fixes)
-------------------------------------------------------------------
fetch_jobs + pagination were fine — the boards really do return jobs. The bug
was the DATE. The list field `releasedDate` is the posting's ORIGINAL creation
date, which on most SmartRecruiters boards is YEARS old (e.g. Justworks 2016,
Palantir 2019, Beamery 2021) even though the postings are still `active: true`.

The pipeline's retention (services/pruner.is_stale) prunes / skips any job whose
`posted_at` is older than settings.prune_days (10). The old code stamped that
ancient `releasedDate` straight into `posted_at`, so virtually every posting was
discarded at crawl time — only the rare board with a genuinely recent
`releasedDate` survived, hence ~1 job retained across 171 companies.

THE FIX
-------
SmartRecruiters exposes no "freshly re-posted / last-activity" date — the detail
endpoint has nothing better than `releasedDate`. So we mirror the Rippling
pattern: stamp `posted_at` ONLY when `releasedDate` is within the retention
window; when it's older we leave `posted_at=None`. pruner.is_stale treats
`posted_at=None` as "kept, not pruned" (`bool(job.posted_at and ...)`), so a live
posting is never thrown away just because its creation date is ancient.

enrich_posted_date also pulls the real full description from jobAd.sections and
the canonical postingUrl — the list API has neither. It's called by the pipeline
for NEW jobs only (post-dedupe), so the per-posting detail call stays cheap.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100&offset={offset}"
DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{jid}"
MAX_PAGES = 10  # cap at 1000 postings per company

# Order of jobAd sections to stitch into the full description.
_SECTION_ORDER = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"smartrecruiters\.com/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


class SmartRecruitersCrawler(BaseCrawler):
    source_name = "smartrecruiters"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("smartrecruiters", "smart_recruiters") or "smartrecruiters.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = self._get(LIST.format(token=token, offset=offset)).json()
            batch = data.get("content", []) or []
            out.extend(batch)
            total = data.get("totalFound", 0) or 0
            offset += len(batch)
            if not batch or offset >= total:
                break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        token = extract_token(company.career_url)
        title = (raw.get("name") or "").strip()
        loc = raw.get("location") or {}
        location = ", ".join(p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p)
        if loc.get("remote"):
            location = (location + " (Remote)").strip()
        job_id = raw.get("id") or raw.get("uuid")
        job_url = f"https://jobs.smartrecruiters.com/{token}/{job_id}" if job_id else ""
        employment_type = ((raw.get("typeOfEmployment") or {}).get("label") or "").strip()
        dept = (raw.get("department") or {}).get("label") or ""
        func = (raw.get("function") or {}).get("label") or ""
        level = (raw.get("experienceLevel") or {}).get("label") or ""
        # Synthesized fallback description; enrich_posted_date replaces it with the
        # real JD pulled from the per-posting detail endpoint.
        description = " · ".join(p for p in [title, dept, func, level, location] if p)
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            # IMPORTANT: do NOT stamp the list `releasedDate` here — it is the
            # original creation date and is usually years old, which would get the
            # (still-active) posting pruned. The real date decision happens in
            # enrich_posted_date.
            posted_at=None,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """Fill description / posted_at / canonical url from the per-posting detail
        endpoint. Called by the pipeline for NEW jobs only (post-dedupe), so it's
        cheap in steady state. On failure the job keeps posted_at=None (kept, not
        pruned) so we never lose a live posting to a transient detail error.

        Freshness rule: `releasedDate` is the posting's CREATION date and is often
        years old even for active postings. We only stamp posted_at when it's
        within the retention window; otherwise we leave it None so pruner.is_stale
        keeps the posting instead of discarding it for an ancient creation date.
        """
        m = re.search(r"smartrecruiters\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9\-]+)", job.job_url or "")
        if not m:
            return job
        token, jid = m.group(1), m.group(2)
        try:
            d = self._get(DETAIL.format(token=token, jid=jid)).json()
        except Exception:  # noqa: BLE001 - keep posted_at=None on failure
            return job

        # Canonical public posting URL (the list API doesn't include it).
        posting_url = d.get("postingUrl") or d.get("applyUrl")
        if posting_url:
            job.job_url = posting_url

        # Real full description from jobAd.sections (HTML); the list API has none.
        sections = (d.get("jobAd") or {}).get("sections") or {}
        parts: List[str] = []
        for key in _SECTION_ORDER:
            sec = sections.get(key) or {}
            text = sec.get("text") or ""
            if text:
                parts.append(text)
        if parts:
            job.description = truncate(clean_html("\n".join(parts)))

        # Only stamp a date that won't get the posting wrongly pruned.
        posted = parse_date(d.get("releasedDate"))
        if posted is not None:
            cutoff = datetime.utcnow() - timedelta(days=settings.prune_days)
            if posted >= cutoff:
                job.posted_at = posted
            # else: ancient creation date on a live posting -> leave None (kept).

        return job
