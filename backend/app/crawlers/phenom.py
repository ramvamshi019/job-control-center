"""
crawlers/phenom.py
------------------
Phenom People (Talent Experience Platform) — per-company ATS. Big footprint:
Snowflake, Zoom, Comcast, T-Mobile, and many enterprise employers.

Detection: the public site is typically at careers.{company}.com or
{company}.jobs and the widget XHR is served from the same host at
    /widgets  (or /api/jobs/search on newer deployments)

Two shapes exist in the wild — we try the newer JSON endpoint first, then
fall back to the /widgets facet-JSON path.

Company.career_url should be the BASE careers host, e.g.:
    "https://careers.snowflake.com"
    "https://careers.tmobile.com"

We accept a bare hostname too ("careers.snowflake.com").

FIELDS available on both endpoints: title, location, url (or job path we
build), postedDate, description (partial). posted_at present on most tenants.

VERIFIED PATTERN 2026-07-31.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

PAGE_SIZE = 50
MAX_PAGES = 10


def _base_url(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    if not s.startswith("http"):
        s = "https://" + s
    # Trim any trailing path so we can append /widgets or /api/jobs/search.
    m = re.match(r"^(https?://[^/]+)", s)
    return m.group(1) if m else s


class PhenomCrawler(BaseCrawler):
    source_name = "phenom"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "phenom" or "phenompeople.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        base = _base_url(company.career_url)
        if not base:
            return []

        out: List[Dict[str, Any]] = []
        seen: set[str] = set()

        # Try newer /api/jobs/search first.
        for page in range(MAX_PAGES):
            params = {
                "from": page * PAGE_SIZE,
                "size": PAGE_SIZE,
                "sortBy": "recent",
            }
            try:
                r = self._get(f"{base}/api/jobs/search", params=params)
                data = r.json()
            except Exception:  # noqa: BLE001
                break
            jobs = data.get("jobs") or data.get("data") or []
            if not jobs:
                break
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                jid = str(j.get("jobId") or j.get("id") or j.get("job_id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append(j)
            if len(jobs) < PAGE_SIZE:
                break

        if out:
            return out

        # Fallback: /widgets (legacy Phenom facet JSON).
        for page in range(MAX_PAGES):
            params = {
                "widget": "search",
                "from": page * PAGE_SIZE,
                "size": PAGE_SIZE,
            }
            try:
                r = self._get(f"{base}/widgets", params=params)
                data = r.json()
            except Exception:  # noqa: BLE001
                break
            jobs = (
                data.get("refineSearch", {}).get("data", {}).get("jobs")
                if isinstance(data, dict)
                else None
            ) or data.get("jobs") or []
            if not jobs:
                break
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                jid = str(j.get("jobId") or j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append(j)
            if len(jobs) < PAGE_SIZE:
                break

        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or raw.get("jobTitle") or "").strip()
        location = (
            raw.get("location")
            or raw.get("primaryLocation")
            or raw.get("city")
            or ""
        )
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location if x)
        location = str(location).strip()

        # url may be absolute or relative.
        base = _base_url(company.career_url)
        url = (raw.get("jobUrl") or raw.get("applyUrl") or raw.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = base + ("" if url.startswith("/") else "/") + url

        description = truncate(clean_html(raw.get("description") or raw.get("jobDescription") or ""))
        posted_at = parse_date(
            raw.get("postedDate")
            or raw.get("posted_date")
            or raw.get("createdDate")
            or raw.get("updatedDate")
        )
        emp_type = str(raw.get("employmentType") or raw.get("jobType") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=emp_type,
            job_url=url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, url),
        )
