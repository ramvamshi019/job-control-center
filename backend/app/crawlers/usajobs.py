"""
crawlers/usajobs.py
-------------------
USAJobs.gov — the official US federal government jobs API. Free, well-doc'd,
generous rate limits.

    GET https://data.usajobs.gov/api/search
    Headers:
        Authorization-Key: {USAJOBS_API_KEY}
        User-Agent: {registered email}
        Host: data.usajobs.gov

Register for a key at https://developer.usajobs.gov/apirequest (auto-issued).
Set USAJOBS_API_KEY and USAJOBS_USER_AGENT (an email) in settings/env.

F-1 caveat: ~90% of federal postings require US citizenship, so raw yield
looks small after downstream filters. Still worth crawling: DOE labs, NASA
JPL, and some contractor-style postings do accept work-authorized non-citizens,
and the sponsor-friendly national-labs seed already lives in this DB.

We filter server-side with JobCategoryCode=2210 (IT Management — the umbrella
for data/software/network/security roles) plus a handful of targeted keyword
queries and dedupe by MatchedObjectId.

VERIFIED PATTERN 2026-07-31 (official docs).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

SEARCH = "https://data.usajobs.gov/api/search"

QUERIES = (
    "data engineer",
    "data scientist",
    "software engineer",
    "machine learning",
    "cloud engineer",
)

PAGE_SIZE = 500
MAX_PAGES_PER_QUERY = 3


class USAJobsCrawler(BaseCrawler):
    source_name = "usajobs"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("usajobs", "usajobs.gov", "usa_jobs") or "usajobs.gov" in s

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization-Key": os.getenv("USAJOBS_API_KEY", ""),
            "User-Agent": os.getenv("USAJOBS_USER_AGENT", "ramvamshikrishna0@gmail.com"),
            "Host": "data.usajobs.gov",
        }

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        if not os.getenv("USAJOBS_API_KEY"):
            return []  # silently no-op until the key is set
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for q in QUERIES:
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                params = {
                    "Keyword": q,
                    "ResultsPerPage": PAGE_SIZE,
                    "Page": page,
                    "JobCategoryCode": "2210",
                }
                try:
                    payload = self._get(SEARCH, params=params, headers=self._headers()).json()
                except Exception:  # noqa: BLE001
                    break
                items = (
                    (payload.get("SearchResult") or {})
                    .get("SearchResultItems")
                    or []
                )
                if not items:
                    break
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj = it.get("MatchedObjectDescriptor") or {}
                    jid = str(it.get("MatchedObjectId") or obj.get("PositionID") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    out.append(obj)
                if len(items) < PAGE_SIZE:
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("PositionTitle") or "").strip()
        job_url = (raw.get("PositionURI") or "").strip()

        locs = raw.get("PositionLocation") or []
        loc_strs = []
        if isinstance(locs, list):
            for L in locs:
                if isinstance(L, dict):
                    name = str(L.get("LocationName") or "").strip()
                    if name:
                        loc_strs.append(name)
        location = "; ".join(loc_strs)

        details = (raw.get("UserArea") or {}).get("Details") or {}
        summary = raw.get("QualificationSummary") or details.get("JobSummary") or ""
        description = truncate(clean_html(str(summary)))

        posted_at = parse_date(raw.get("PublicationStartDate"))

        org = str(raw.get("OrganizationName") or raw.get("DepartmentName") or "US Federal Government").strip()

        emp_type_list = raw.get("PositionSchedule") or []
        emp_type = ""
        if isinstance(emp_type_list, list) and emp_type_list:
            first = emp_type_list[0]
            if isinstance(first, dict):
                emp_type = str(first.get("Name") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=org,
            location=location,
            employment_type=emp_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(org, title, location, job_url),
        )
