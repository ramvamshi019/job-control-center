"""
crawlers/jazzhr.py
------------------
JazzHR (Resumator) — per-company ATS, popular in SMB / mid-market. No auth.

Public candidate portal is at:
    https://{tenant}.applytojob.com

Two feed styles are exposed by most tenants — we try in order:
    1) https://{tenant}.applytojob.com/apply/jobs/feed.rss   (RSS)
    2) https://{tenant}.applytojob.com/apply/jobs/feed       (XML variant)
    3) HTML listing scrape as last resort (skipped for now — low ROI)

company.career_url accepts a bare tenant slug ("acmecorp") or a full URL.

Fields available: title, location, url, description, posted_date (RSS pubDate).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate


def _tenant(career_url: str) -> str:
    s = (career_url or "").strip()
    if not s:
        return ""
    if "applytojob.com" in s.lower() or "jazz.co" in s.lower():
        url = s if s.startswith("http") else "https://" + s
        host = urlparse(url).hostname or ""
        return host.split(".")[0] if host else ""
    if "/" not in s and "." not in s:
        return s
    return ""


class JazzHRCrawler(BaseCrawler):
    source_name = "jazzhr"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("jazzhr", "jazz") or "applytojob.com" in s or "jazz.co" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        t = _tenant(company.career_url)
        if not t:
            return []

        for suffix in ("/apply/jobs/feed.rss", "/apply/jobs/feed"):
            try:
                r = self._get(f"https://{t}.applytojob.com{suffix}")
            except Exception:  # noqa: BLE001
                continue
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                continue
            items = root.findall(".//item")
            if not items:
                continue
            return [
                {
                    "title": (item.findtext("title") or "").strip(),
                    "link": (item.findtext("link") or "").strip(),
                    "description": (item.findtext("description") or "").strip(),
                    "pubDate": (item.findtext("pubDate") or "").strip(),
                    "location": _extract_location(item.findtext("description") or ""),
                }
                for item in items
            ]
        return []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        job_url = (raw.get("link") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        posted_at = parse_date(raw.get("pubDate"))
        location = (raw.get("location") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )


def _extract_location(desc_html: str) -> str:
    """JazzHR RSS descriptions often lead with 'Location: City, ST'.
    Best-effort pull; falls back to empty string."""
    if not desc_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", desc_html)
    m = re.search(r"Location:\s*([^\n<|]+)", text, re.I)
    return m.group(1).strip() if m else ""
