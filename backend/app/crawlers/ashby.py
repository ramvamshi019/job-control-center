"""
crawlers/ashby.py
-----------------
Ashby public job board API (no key needed):
    https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true

Accepts in company.career_url:
    openai
    https://jobs.ashbyhq.com/openai
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"ashbyhq\.com/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


class AshbyCrawler(BaseCrawler):
    source_name = "ashby"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "ashby" or "ashbyhq.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token)).json()
        return data.get("jobs", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        employment_type = (raw.get("employmentType") or "").strip()
        job_url = raw.get("jobUrl") or raw.get("applyUrl") or ""
        description = truncate(
            clean_html(raw.get("descriptionHtml") or raw.get("descriptionPlain") or "")
        )
        posted_at = parse_date(raw.get("publishedAt") or raw.get("updatedAt"))

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
