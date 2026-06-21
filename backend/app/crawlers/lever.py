"""
crawlers/lever.py
-----------------
Lever public postings API (no key needed):
    https://api.lever.co/v0/postings/{token}?mode=json

Accepts in company.career_url:
    netflix
    https://jobs.lever.co/netflix
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://api.lever.co/v0/postings/{token}?mode=json"


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"lever\.co/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


class LeverCrawler(BaseCrawler):
    source_name = "lever"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "lever" or "lever.co" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token)).json()
        # Lever returns a JSON list directly.
        return data if isinstance(data, list) else []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("text") or "").strip()
        cats = raw.get("categories") or {}
        location = (cats.get("location") or "").strip()
        employment_type = (cats.get("commitment") or "").strip()  # e.g. "Full-time"
        job_url = raw.get("hostedUrl") or raw.get("applyUrl") or ""
        description = truncate(
            clean_html(raw.get("descriptionPlain") or raw.get("description") or "")
        )
        posted_at = parse_date(raw.get("createdAt"))

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
