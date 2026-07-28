"""
crawlers/adzuna.py
------------------
Adzuna aggregator API — pulls listings from Indeed, Reed, CareerBuilder,
Monster and dozens of other feeds through a single public JSON endpoint.

    https://developer.adzuna.com/  (free tier: 500 calls/day)

Ram picked Adzuna as the top new source because direct Indeed scraping is
blocked by Cloudflare — Adzuna is how you get Indeed listings (indirect)
without a paid scraper.

KEY MANAGEMENT:
    settings.adzuna_app_id + adzuna_app_key are read from environment /
    backend/.env at startup. If either is empty the crawler silently returns
    []; nothing errors, nothing spams the logs. So this file can ship BEFORE
    Ram signs up — activation is a two-line .env edit + container restart.

FREE-TIER BUDGET:
    500 calls/day. With MAX_PAGES_PER_QUERY=3 and PAGE_SIZE=50 across
    QUERIES=(6) that's 18 calls per crawl. At priority='high' (4h interval =
    6 crawls/day) that's 108 calls/day — well inside 500 with headroom for
    the aggregator's ~1s response time.

DEDUPE:
    Adzuna's `id` is a stable per-listing identifier we collapse duplicates
    on within a single fetch (same job appears across multiple queries).
    Cross-source dedupe (same posting from Adzuna and Greenhouse/Lever)
    happens later in scheduler.persist_company_jobs via find_duplicate.

LOCATION / DATE:
    location.display_name is a clean "City, State" string.
    created is ISO-8601 with UTC timezone — a real posted date, no fallback.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

API = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"

# Ram's actual target scope — mirrors amazon.py to keep signal density high.
# Kept short because each entry costs `MAX_PAGES_PER_QUERY` API calls per run.
QUERIES = (
    "data engineer",
    "software engineer",
    "machine learning engineer",
    "cloud engineer",
    "backend engineer",
    "site reliability engineer",
)

PAGE_SIZE = 50            # 50 is the API's soft page cap
MAX_PAGES_PER_QUERY = 3   # 3 × 50 = 150 results per query, ~900/run


def _parse_iso(v: str | None) -> Optional[datetime]:
    """Adzuna's `created` is YYYY-MM-DDTHH:MM:SSZ. Return None on any
    parse failure rather than stamping crawl-time."""
    if not v:
        return None
    try:
        # datetime.fromisoformat handles the Z suffix from 3.11+.
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


class AdzunaCrawler(BaseCrawler):
    source_name = "adzuna"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "adzuna" or "adzuna.com" in s

    def _configured(self) -> bool:
        """No-op crawler when the free-tier keys aren't provided. Not an
        error — just a soft-skip so ops can ship this file before signup."""
        return bool(settings.adzuna_app_id and settings.adzuna_app_key)

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        if not self._configured():
            return []
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for q in QUERIES:
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                params = {
                    "app_id": settings.adzuna_app_id,
                    "app_key": settings.adzuna_app_key,
                    "results_per_page": PAGE_SIZE,
                    "what": q,
                    # "content-type": "application/json" -> already default
                    "sort_by": "date",
                }
                try:
                    data = self._get(API.format(page=page), params=params).json()
                except Exception:  # noqa: BLE001 - one query can't break the crawl
                    break
                results = data.get("results") or []
                if not results:
                    break
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    jid = str(r.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    out.append(r)
                if len(results) < PAGE_SIZE:
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        # Adzuna nests company + location under sub-objects.
        employer = (
            ((raw.get("company") or {}).get("display_name") or "").strip()
            or "Unknown"
        )
        location = ((raw.get("location") or {}).get("display_name") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        # `redirect_url` is Adzuna's tracked link that resolves to the actual
        # employer application URL — that's what we want the user to open.
        job_url = (raw.get("redirect_url") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type=(raw.get("contract_time") or "").strip(),
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=_parse_iso(raw.get("created")),
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
