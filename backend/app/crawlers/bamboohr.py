"""
crawlers/bamboohr.py
--------------------
BambooHR public careers list (no key needed):
    https://{token}.bamboohr.com/careers/list   -> {"result": [ ... ]}

`token` is the company subdomain. Accepts a bare token or any *.bamboohr.com URL.
The list endpoint has no job description or date, so we synthesize a short
description from title/department/location and treat first-seen as posted date
(so retention/pruning still works).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import make_hash

DETAIL = "{job_url}/detail"  # BambooHR per-job detail endpoint has the real datePosted

API = "https://{token}.bamboohr.com/careers/list"


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"([A-Za-z0-9_-]+)\.bamboohr\.com", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _location(raw: Dict[str, Any]) -> str:
    loc = raw.get("location") or {}
    ats = raw.get("atsLocation") or {}
    parts = [loc.get("city") or ats.get("city"),
             loc.get("state") or ats.get("state") or ats.get("province"),
             ats.get("country")]
    s = ", ".join(p for p in parts if p)
    if not s and raw.get("isRemote"):
        s = "Remote"
    return s


class BambooHRCrawler(BaseCrawler):
    source_name = "bamboohr"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "bamboohr" or "bamboohr.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token)).json()
        return data.get("result", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        token = extract_token(company.career_url)
        title = (raw.get("jobOpeningName") or "").strip()
        location = _location(raw)
        job_id = raw.get("id")
        job_url = f"https://{token}.bamboohr.com/careers/{job_id}" if job_id else ""
        employment_type = (raw.get("employmentStatusLabel") or "").strip()
        dept = raw.get("departmentLabel") or ""
        description = " · ".join(p for p in [title, dept, location,
                                             "Remote" if raw.get("isRemote") else ""] if p)
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=datetime.utcnow(),  # provisional; enrich_posted_date() fixes it
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """The list API has no date, so normalize_job stamps 'now'. The per-job
        detail endpoint DOES carry the real datePosted — fetch it here. Called by
        the pipeline for NEW jobs only (post-dedupe), so it's cheap in steady
        state. On failure, the provisional 'now' is left as a safe fallback."""
        if not job.job_url:
            return job
        try:
            data = self._get(DETAIL.format(job_url=job.job_url)).json()
            dp = (data.get("result", {}).get("jobOpening", {}) or {}).get("datePosted")
            parsed = parse_date(dp) if dp else None
            if parsed:
                job.posted_at = parsed
        except Exception:  # noqa: BLE001 - keep provisional date if detail unavailable
            pass
        return job
