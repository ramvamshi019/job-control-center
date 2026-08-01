"""
crawlers/simplify.py
--------------------
SimplifyJobs GitHub repos — the community-maintained early-career job lists.
No auth, no key; served from raw.githubusercontent.com.

    https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
    https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json
    https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json

SENTINEL SOURCE (one Company row, three feeds combined). Each listings.json
entry has: id, company_name, title, locations[], url, date_posted (unix),
season, sponsorship, active, source. Curated + updated daily by GH Actions —
very high precision for new-grad/internship roles that JCC otherwise sees
late (or never, when the company is on a niche ATS).

We drop entries with active=false so closed roles don't pollute Best Matches,
and we keep the SimplifyJobs `sponsorship` field in the description so the
filter engine can see the visa signal.

VERIFIED LIVE 2026-07-31.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import make_hash, truncate

FEEDS = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
]


class SimplifyCrawler(BaseCrawler):
    source_name = "simplify"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("simplify", "simplifyjobs", "simplify_jobs")

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for url in FEEDS:
            try:
                items = self._get(url).json()
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("active") is False:
                    continue
                if it.get("is_visible") is False:
                    continue
                key = str(it.get("id") or it.get("url") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(it)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        employer = (raw.get("company_name") or "").strip() or "Unknown"

        locs = raw.get("locations") or []
        if isinstance(locs, list):
            location = ", ".join(str(x) for x in locs if x)
        else:
            location = str(locs).strip()

        job_url = (raw.get("url") or "").strip()

        ts = raw.get("date_posted")
        posted_at = None
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                posted_at = datetime.utcfromtimestamp(int(ts))
            except (OverflowError, OSError, ValueError):
                posted_at = None

        # Preserve sponsorship + season signal so filters and rankers can see it.
        sponsorship = str(raw.get("sponsorship") or "").strip()
        season = str(raw.get("season") or "").strip()
        parts = []
        if sponsorship:
            parts.append(f"Sponsorship: {sponsorship}")
        if season:
            parts.append(f"Season: {season}")
        description = truncate(" | ".join(parts)) if parts else ""

        emp_type = "internship" if "intern" in title.lower() or "intern" in season.lower() else "full-time"

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
