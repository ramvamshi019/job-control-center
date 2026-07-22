"""
crawlers/ukg.py
---------------
UKG Pro Recruiting (formerly UltiPro) public job boards — no key needed. Like
Workday there is no single token: a board is identified by host + client code +
board GUID, e.g.

    https://recruiting2.ultipro.com/OSI1000OSIG/JobBoard/5c31fa30-0b12-4718-abf6-664d6271de12

The board's own front-end calls a public POST search endpoint:

    POST {board}/JobBoardView/LoadSearchResults
    body: {"opportunitySearch": {"Top": N, "Skip": N, "QueryString": "",
                                 "OrderBy": [...], "Filters": []}}
    -> {"opportunities": [...], "totalCount": N}

WHY THE ODD REQUEST BODY
------------------------
The endpoint answers 200 to anything, but silently returns zero results unless
the payload is wrapped in `opportunitySearch` with PascalCase keys — a flat
{"Top":..,"Skip":..} body yields totalCount 0. Don't "simplify" it.

Accepts in company.career_url:
    https://recruiting2.ultipro.com/{code}/JobBoard/{guid}
    recruiting2.ultipro.com|{code}|{guid}
    {code}|{guid}                      (host defaults to recruiting2)

POSTED DATE
-----------
`PostedDate` is the posting's publish timestamp (boards return a spread of
distinct timestamps down to the second). There is no separate "updated" field to
confuse it with. If it is missing we leave posted_at=None instead of stamping
crawl time — a made-up "posted today" would make an ancient req look brand new.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

PAGE = 50
MAX_PAGES = 20  # cap ~1000 postings per company so big boards stay bounded
DEFAULT_HOST = "recruiting2.ultipro.com"

_BOARD_URL = re.compile(
    r"(?:https?://)?([a-z0-9.-]*ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
    re.I,
)


def parse_career_url(career_url: str) -> Optional[Tuple[str, str, str]]:
    """Return (host, client_code, board_id) from a board URL or a pipe string."""
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return None
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return DEFAULT_HOST, parts[0], parts[1]
        return None
    m = _BOARD_URL.search(s)
    if m:
        return m.group(1).lower(), m.group(2), m.group(3)
    return None


def _location(raw: Dict[str, Any]) -> str:
    """First location as 'City, ST' — falls back to the board's display name."""
    locations = raw.get("Locations") or []
    if not locations:
        return ""
    first = locations[0] or {}
    addr = first.get("Address") or {}
    state = (addr.get("State") or {}).get("Code") or ""
    parts = [p for p in [addr.get("City"), state] if p]
    if parts:
        return ", ".join(parts)
    return (first.get("LocalizedName") or "").strip()


class UKGCrawler(BaseCrawler):
    source_name = "ukg"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("ukg", "ultipro", "ukg_pro") or "ultipro.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        parsed = parse_career_url(company.career_url)
        if not parsed:
            return []
        host, code, board_id = parsed
        base = f"https://{host}/{code}/JobBoard/{board_id}"
        url = f"{base}/JobBoardView/LoadSearchResults"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        out: List[Dict[str, Any]] = []
        skip = 0
        for _ in range(MAX_PAGES):
            time.sleep(max(0.0, settings.crawl_delay_seconds))
            resp = self.session.post(
                url, headers=headers,
                json={"opportunitySearch": {
                    "Top": PAGE, "Skip": skip, "QueryString": "",
                    "OrderBy": [{"Value": "postedDateDesc",
                                 "PropertyName": "PostedDate", "Ascending": False}],
                    "Filters": [],
                }},
                timeout=settings.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("opportunities", []) or []
            out.extend(batch)
            total = data.get("totalCount", 0) or 0
            skip += PAGE
            if not batch or skip >= total:
                break

        # stash the board base so normalize_job can build public job URLs
        for j in out:
            j["_base"] = base
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("Title") or "").strip()
        location = _location(raw)
        if raw.get("JobLocationType") == 3:  # UKG's "remote" location type
            location = (location + " (Remote)").strip()

        opp_id = raw.get("Id") or ""
        base = raw.get("_base", "")
        job_url = f"{base}/OpportunityDetail?opportunityId={opp_id}" if base and opp_id else ""

        category = raw.get("JobCategoryName") or ""
        brief = clean_html(raw.get("BriefDescription") or "")
        description = truncate(brief) if brief else " · ".join(
            p for p in [title, category, location] if p)

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="full-time" if raw.get("FullTime") else "",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=parse_date(raw.get("PostedDate")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
