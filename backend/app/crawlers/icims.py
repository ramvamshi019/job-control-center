"""
crawlers/icims.py
-----------------
iCIMS hosted career portals. Unlike Greenhouse/Lever/Ashby there is no clean
public JSON API, but the iframe job-search page is plain, paginated HTML:

    https://careers-{token}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}

Each result row carries the title (<h3>), a "Job Locations" span (e.g.
"US-MO-St. Louis"), a short description and the job URL. We page through with
`pr=0,1,2,…` until a page returns no rows (or the page cap is hit).

`token` is the portal subdomain. Accepts a bare token ("360care") or any
careers-{token}.icims.com / {token}.icims.com URL.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

PAGE = "https://careers-{token}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}"
MAX_PAGES = 5  # 50 rows/page -> cap ~250 postings per company (keeps the
# HTML-paged iCIMS crawl from bottlenecking the 24/7 sweep; most boards are
# smaller than this and the newest postings come first).
JOB_HREF = re.compile(r"/jobs/\d+/[^\"']+/job", re.I)


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"careers-([A-Za-z0-9_-]+)\.icims\.com", s)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z0-9_-]+)\.icims\.com", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


class IcimsCrawler(BaseCrawler):
    source_name = "icims"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "icims" or "icims.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        out: List[Dict[str, Any]] = []
        seen_urls: set = set()
        for page in range(MAX_PAGES):
            resp = self._get(PAGE.format(token=token, page=page))
            rows = self._parse_rows(resp.text)
            new = [r for r in rows if r["job_url"] not in seen_urls]
            for r in new:
                seen_urls.add(r["job_url"])
            out.extend(new)
            # Stop when a page yields no rows, or none we hadn't already seen
            # (iCIMS clamps out-of-range pages back to the last real page).
            if not new:
                break
        return out

    @staticmethod
    def _parse_rows(html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "lxml")
        rows: List[Dict[str, Any]] = []
        for a in soup.find_all("a", href=JOB_HREF):
            href = a.get("href", "").split("?")[0]
            h3 = a.find("h3")
            title = (h3.get_text(" ", strip=True) if h3 else "") or \
                re.sub(r"^\d+\s*-\s*", "", a.get("title", "")).strip()
            if not title:
                continue
            # The row container holds the location span + description div.
            container = a
            for _ in range(4):
                if container.parent:
                    container = container.parent
                if container.find(class_="description"):
                    break
            location = ""
            label = container.find("span", string=re.compile(r"Job Location", re.I))
            if label:
                sib = label.find_next("span")
                if sib:
                    location = sib.get_text(" ", strip=True)
            desc_div = container.find(class_="description")
            description = clean_html(desc_div.get_text(" ", strip=True)) if desc_div else title
            rows.append({
                "title": title,
                "job_url": href,
                "location": location,
                "description": description,
            })
        return rows

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=raw.get("job_url") or "",
            source=self.source_name,
            description=truncate(raw.get("description") or title),
            posted_at=None,  # iCIMS search rows don't expose a post date
            raw_data_hash=make_hash(company.name, title, location, raw.get("job_url") or ""),
        )
