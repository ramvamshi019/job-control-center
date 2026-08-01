"""
crawlers/microsoft.py
---------------------
Microsoft Careers — undocumented but publicly accessible JSON API used by the
jobs.careers.microsoft.com front-end. No auth, no key.

    https://gcsservices.careers.microsoft.com/search/api/v1/search
      ?q=&lc=United%20States&l=en_us&pg=1&pgSz=20&o=Recent&flt=true

SENTINEL SOURCE (single Company row). Microsoft is a top-5 H-1B sponsor in the
US; direct API integration puts data-eng/SWE/ML roles in front of Ram the same
day they post.

pgSz caps at 20. We paginate until an empty page or MAX_PAGES, filtering by
country=United States server-side.

Response shape (v1/search):
    {"operationResult": {"result": {"jobs": [...], "totalJobs": N, ...}}}
Per job: jobId, title, primaryWorkLocation.{city,state,country},
    postingDate, properties.{description,employmentType}, ...

Full description requires a follow-up call to /search/api/v1/job/{jobId} — the
list only carries a short summary. We include the summary; enrichment can fill
in the rest later if needed.

VERIFIED PATTERN 2026-07-31 (docs + third-party analysis).
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

SEARCH = "https://gcsservices.careers.microsoft.com/search/api/v1/search"
JOB_URL_TEMPLATE = "https://jobs.careers.microsoft.com/global/en/job/{job_id}"

QUERIES = (
    "data engineer",
    "software engineer",
    "machine learning",
    "cloud engineer",
    "backend engineer",
    "site reliability engineer",
)

PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 10  # up to 200 jobs per query


def _pick_location(job: Dict[str, Any]) -> str:
    primary = job.get("primaryWorkLocation") or {}
    if isinstance(primary, dict):
        city = str(primary.get("city") or "").strip()
        state = str(primary.get("state") or "").strip()
        country = str(primary.get("country") or "").strip()
        parts = [p for p in (city, state, country) if p]
        if parts:
            return ", ".join(parts)
    locs = job.get("workLocations") or []
    if isinstance(locs, list) and locs:
        first = locs[0]
        if isinstance(first, dict):
            return ", ".join(str(x) for x in (first.get("city"), first.get("state"), first.get("country")) if x)
        return str(first)
    return ""


class MicrosoftCareersCrawler(BaseCrawler):
    source_name = "microsoft"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("microsoft", "microsoft_careers", "msft") or "careers.microsoft.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for q in QUERIES:
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                params = {
                    "q": q,
                    "lc": "United States",
                    "l": "en_us",
                    "pg": page,
                    "pgSz": PAGE_SIZE,
                    "o": "Recent",
                    "flt": "true",
                }
                try:
                    payload = self._get(SEARCH, params=params).json()
                except Exception:  # noqa: BLE001
                    break
                result = (payload.get("operationResult") or {}).get("result") or {}
                batch = result.get("jobs") or []
                if not batch:
                    break
                for j in batch:
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("jobId") or j.get("id") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    out.append(j)
                if len(batch) < PAGE_SIZE:
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        jid = str(raw.get("jobId") or raw.get("id") or "").strip()
        job_url = JOB_URL_TEMPLATE.format(job_id=jid) if jid else ""
        location = _pick_location(raw)
        posted_at = parse_date(raw.get("postingDate") or raw.get("posted_date"))

        props = raw.get("properties") or {}
        description = ""
        emp_type = ""
        if isinstance(props, dict):
            description = truncate(clean_html(props.get("description") or ""))
            emp_type = str(props.get("employmentType") or "").strip()
        if not description:
            description = truncate(clean_html(raw.get("summary") or ""))

        return Job(
            company_id=company.id,
            title=title,
            company_name="Microsoft",
            location=location,
            employment_type=emp_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash("Microsoft", title, location, job_url),
        )
