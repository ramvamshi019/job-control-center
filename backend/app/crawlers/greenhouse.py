"""
crawlers/greenhouse.py
----------------------
Greenhouse public job board API (no key needed):
    https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

`token` is the board id. We accept any of these in company.career_url:
    stripe
    https://boards.greenhouse.io/stripe
    https://job-boards.greenhouse.io/stripe
    https://stripe.com/jobs  (we try to read the last path part — may need fixing)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def extract_token(career_url: str) -> str:
    """Get the greenhouse board token from a URL or a bare token."""
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s  # already a bare token
    return s.split("/")[-1]


class GreenhouseCrawler(BaseCrawler):
    source_name = "greenhouse"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "greenhouse" or "greenhouse.io" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token)).json()
        return data.get("jobs", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = ((raw.get("location") or {}).get("name") or "").strip()
        job_url = raw.get("absolute_url") or ""
        description = truncate(clean_html(raw.get("content") or ""))
        # first_published FIRST. `updated_at` is the last time anyone *edited* the
        # posting, and recruiters re-touch old reqs constantly — reading it made
        # 2-year-old jobs show up as "posted today" (measured: 62% of greenhouse
        # jobs JCC called fresh were older than 2 weeks, 43% older than 60 days;
        # worst was 778 days). Both fields come back in the same list response, so
        # preferring the real publish date costs no extra requests.
        posted_at = parse_date(raw.get("first_published") or raw.get("updated_at"))

        # employment_type isn't always present; infer lightly from metadata.
        employment_type = ""
        for meta in raw.get("metadata") or []:
            if "employment" in str(meta.get("name", "")).lower():
                employment_type = str(meta.get("value") or "")

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
