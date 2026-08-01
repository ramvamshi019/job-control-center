"""
crawlers/aijobs_net.py
----------------------
ai-jobs.net — AI/ML/data-only public JSON feed. No auth, no key.

    https://ai-jobs.net/api/list-jobs/

SENTINEL SOURCE: one Company row, one JSON pull. The feed returns up to the
200 most-recent postings and refreshes every ~2 hours, so a normal livewatch
sweep will see the full window each run.

Fields present: title, company, location, url, published_at, description, tags,
salary. Real posted_at → no crawl-time fallback needed.

Niche: AI/ML/data. Small volume (~50-200 fresh/day) but tightly on-topic for
data-eng search, and many postings are also on Greenhouse/Lever — dedupe by
job_url keeps costs low.

VERIFIED LIVE 2026-07-31.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

FEED = "https://ai-jobs.net/api/list-jobs/"


class AIJobsNetCrawler(BaseCrawler):
    source_name = "aijobs_net"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("aijobs_net", "ai-jobs.net", "aijobs") or "ai-jobs.net" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        data = self._get(FEED).json()
        # API returns {"jobs": [...]} — accept either that or a bare list.
        if isinstance(data, dict):
            return data.get("jobs") or data.get("data") or []
        return data if isinstance(data, list) else []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or raw.get("job_title") or "").strip()
        employer = (raw.get("company") or raw.get("company_name") or "").strip() or "Unknown"

        # location may be a string or a list of city strings.
        loc_raw = raw.get("location") or raw.get("locations") or ""
        if isinstance(loc_raw, list):
            location = ", ".join(str(x) for x in loc_raw if x)
        else:
            location = str(loc_raw).strip()

        job_url = (raw.get("url") or raw.get("apply_url") or raw.get("job_url") or "").strip()
        description = truncate(clean_html(raw.get("description") or raw.get("description_html") or ""))
        posted_at = parse_date(
            raw.get("published_at")
            or raw.get("posted_at")
            or raw.get("published")
            or raw.get("created_at")
        )
        emp_type = (raw.get("employment_type") or raw.get("job_type") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type=emp_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
