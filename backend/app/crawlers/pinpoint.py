"""
crawlers/pinpoint.py
--------------------
Pinpoint public careers feed (no key needed):
    https://{token}.pinpointhq.com/postings.json   -> {"data": [ ... ]}

`token` is the company subdomain. Accepts a bare token or any *.pinpointhq.com
URL.

Posted date: Pinpoint's public feed carries NO posted/published field (only an
optional application `deadline_at`). So like the workday/bamboohr crawlers we
stamp crawl-time as a provisional posted_at — retention/pruning still works, but
this source can't distinguish a fresh posting from an old one.

Feed shape (verified live 2026-07-27):
    { "data": [ {
        "title": "...",
        "url": "https://{token}.pinpointhq.com/en/postings/<uuid>",
        "description": "<full HTML>",
        "location": {"id":.., "city":"..", "name":"..", "province":".."},
        "deadline_at": null
    }, ... ] }
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

API = "https://{token}.pinpointhq.com/postings.json"
_HEADERS = {"Accept": "application/json, */*"}


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"([A-Za-z0-9_-]+)\.pinpointhq\.com", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _location(raw: Dict[str, Any]) -> str:
    loc = raw.get("location") or {}
    if not isinstance(loc, dict):
        return ""
    # Prefer the full display `name`; fall back to city/province.
    if loc.get("name"):
        return str(loc["name"]).strip()
    return ", ".join(str(p).strip() for p in (loc.get("city"), loc.get("province")) if p)


class PinpointCrawler(BaseCrawler):
    source_name = "pinpoint"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "pinpoint" or "pinpointhq.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token), headers=_HEADERS).json()
        if not isinstance(data, dict):
            return []
        return data.get("data", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = _location(raw)
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or title))
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=datetime.utcnow(),  # no date in feed; crawl-time fallback
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
