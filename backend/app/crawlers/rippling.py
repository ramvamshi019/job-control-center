"""
crawlers/rippling.py
--------------------
Rippling ATS public job board (no key needed):
    list:   https://ats.rippling.com/api/v2/board/{token}/jobs?page=0&pageSize=50
    detail: https://ats.rippling.com/api/v2/board/{token}/jobs/{job_id}

`token` is the board slug, e.g. "cars-and-bids-job-board" from
    https://ats.rippling.com/cars-and-bids-job-board/jobs

The list API returns only id/name/url/locations — no posted date or description.
We stamp those from the per-job detail endpoint in enrich_posted_date(), which
the pipeline calls for NEW jobs only (post-dedupe), so it stays cheap — same
pattern as BambooHR. Rippling skews to well-funded US tech startups, exactly the
entry/mid data-engineer segment, and these roles often reach aggregators late.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = "https://ats.rippling.com/api/v2/board/{token}/jobs?page={page}&pageSize=50"
DETAIL = "https://ats.rippling.com/api/v2/board/{token}/jobs/{jid}"
MAX_PAGES = 40  # safety cap (50 jobs/page -> 2000 jobs)


def extract_token(career_url: str) -> str:
    """Board slug from a Rippling URL or a bare slug."""
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"ats\.rippling\.com/([^/?#]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s  # already a bare slug
    return s.split("/")[-1]


class RipplingCrawler(BaseCrawler):
    source_name = "rippling"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "rippling" or "ats.rippling.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        out: List[Dict[str, Any]] = []
        page = 0
        while page < MAX_PAGES:
            data = self._get(LIST.format(token=token, page=page)).json()
            items = data.get("items", []) or []
            out.extend(items)
            total_pages = data.get("totalPages") or 1
            page += 1
            if page >= total_pages or not items:
                break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("name") or "").strip()
        locs = raw.get("locations") or []
        location = ", ".join((l.get("name") or "").strip() for l in locs if l.get("name"))
        job_url = raw.get("url") or ""
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description="",      # filled by enrich_posted_date (detail call)
            posted_at=None,      # filled by enrich_posted_date
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """List API has no date/description — fetch them from the per-job detail
        endpoint. Called by the pipeline for NEW jobs only (post-dedupe), so it's
        cheap in steady state. On failure the job keeps posted_at=None (kept, not
        pruned) so we never lose a posting to a transient detail-call error."""
        m = re.search(r"ats\.rippling\.com/([^/]+)/jobs/([A-Za-z0-9\-]+)", job.job_url or "")
        if not m:
            return job
        token, jid = m.group(1), m.group(2)
        try:
            d = self._get(DETAIL.format(token=token, jid=jid)).json()
        except Exception:  # noqa: BLE001 - keep None on failure
            return job
        posted = parse_date(d.get("createdOn"))
        if posted:
            job.posted_at = posted
        # description is {"role": "<html>", "company": "<html>"} — role is the JD.
        desc = d.get("description")
        if isinstance(desc, dict):
            raw = "\n".join(p for p in (desc.get("role"), desc.get("company")) if p)
        elif isinstance(desc, str):
            raw = desc
        else:
            raw = ""
        if raw:
            job.description = truncate(clean_html(raw))
        et = d.get("employmentType")
        if isinstance(et, dict):
            job.employment_type = str(et.get("id") or et.get("label") or "")
        return job
