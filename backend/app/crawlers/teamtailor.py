"""
crawlers/teamtailor.py
----------------------
Teamtailor public career-site feed (no key needed):
    https://{token}.teamtailor.com/jobs.json   -> JSON Feed 1.1 object

The official api.teamtailor.com needs an API key, but every Teamtailor career
site also serves an unauthenticated jsonfeed.org 1.1 document at /jobs.json (and
an RSS twin at /jobs.rss). We read the JSON feed.

`token` is the career-site subdomain and MAY include a region segment
(e.g. "thestudio.na" -> thestudio.na.teamtailor.com), so we capture everything
left of ".teamtailor.com". Accepts a bare token or any *.teamtailor.com URL.

Feed shape (verified live 2026-07-27):
    { "version": "https://jsonfeed.org/version/1.1", "items": [ {
        "id": "<uuid>", "title": "...", "url": "https://.../jobs/657597-...",
        "content_html": "<full HTML body>",
        "date_published": "2026-07-03T13:07:16+02:00",
        "_jobposting": { schema.org JobPosting: jobLocation[], jobLocationType }
    }, ... ] }
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://{token}.teamtailor.com/jobs.json"

# The jsonfeed is served for browsers/readers; ask for JSON explicitly.
_HEADERS = {"Accept": "application/json, application/feed+json, */*"}


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    # Capture the FULL subdomain (may contain dots, e.g. "thestudio.na").
    m = re.search(r"([A-Za-z0-9_.-]+)\.teamtailor\.com", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _location(raw: Dict[str, Any]) -> str:
    """Teamtailor puts location inside the embedded schema.org JobPosting under
    _jobposting.jobLocation (a Place, or list of Places, each with an address).
    Fall back to the item's flat `_geoloc`/tags nothing-known -> ""."""
    jp = raw.get("_jobposting") or {}
    locs = jp.get("jobLocation")
    if isinstance(locs, dict):
        locs = [locs]
    parts: List[str] = []
    for place in locs or []:
        addr = (place or {}).get("address") or {}
        one = ", ".join(
            p for p in (addr.get("addressLocality"),
                        addr.get("addressRegion"),
                        addr.get("addressCountry")) if p
        )
        if one:
            parts.append(one)
    location = " / ".join(dict.fromkeys(parts))  # dedupe, keep order
    if str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        location = (location + " (Remote)").strip()
    return location


class TeamtailorCrawler(BaseCrawler):
    source_name = "teamtailor"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "teamtailor" or "teamtailor.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token), headers=_HEADERS).json()
        if not isinstance(data, dict):
            return []
        return data.get("items", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = _location(raw)
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("content_html") or ""))
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            # Real publish date from the feed; None on junk (never now()).
            posted_at=parse_date(raw.get("date_published")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
