"""
crawlers/workday.py
-------------------
Workday public job board API (no key needed). Workday has no single token: each
company is identified by tenant + data-center pod + site path, e.g.
    nvidia.wd5.myworkdayjobs.com/.../NVIDIAExternalCareerSite

We store that in company.career_url as a pipe string "tenant|dc|site"
(e.g. "nvidia|wd5|NVIDIAExternalCareerSite"), or a full myworkdayjobs.com URL.

API (POST, paginated 20 at a time):
    https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
The list gives postedOn as a relative string ("Posted 7 Days Ago"), which we
convert to a real date so the 10-day retention/pruning still works.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import make_hash

PAGE = 20
MAX_PAGES = 15  # cap ~300 jobs/company to keep big crawls bounded


def parse_career_url(career_url: str) -> Optional[Tuple[str, str, str]]:
    """Return (tenant, dc, site) from 'tenant|dc|site' or a myworkdayjobs URL."""
    s = (career_url or "").strip()
    if "|" in s:
        parts = s.split("|")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return None
    m = re.search(r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)", s)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _posted_at(posted_on: str) -> datetime:
    """Convert 'Posted 7 Days Ago' / 'Posted Today' to an approximate datetime."""
    now = datetime.utcnow()
    t = (posted_on or "").lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)
    return now  # unknown -> treat as just seen


class WorkdayCrawler(BaseCrawler):
    source_name = "workday"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "workday" or "myworkdayjobs.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        parsed = parse_career_url(company.career_url)
        if not parsed:
            return []
        tenant, dc, site = parsed
        url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            time.sleep(max(0.0, settings.crawl_delay_seconds))
            resp = self.session.post(
                url, headers=headers,
                json={"limit": PAGE, "offset": offset, "searchText": "", "appliedFacets": {}},
                timeout=settings.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("jobPostings", []) or []
            out.extend(batch)
            total = data.get("total", 0)
            offset += PAGE
            if not batch or offset >= total:
                break
        # stash host info for normalize_job (job URLs)
        for j in out:
            j["_host"] = f"{tenant}.{dc}.myworkdayjobs.com"
            j["_site"] = site
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = (raw.get("locationsText") or "").strip()
        path = raw.get("externalPath") or ""
        host = raw.get("_host", "")
        site = raw.get("_site", "")
        job_url = f"https://{host}/en-US/{site}{path}" if host and path else ""
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=" · ".join(p for p in [title, location] if p),
            posted_at=_posted_at(raw.get("postedOn") or ""),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
