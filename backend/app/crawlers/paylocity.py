"""
crawlers/paylocity.py
---------------------
Paylocity Recruiting public job boards (no key needed). Every board is keyed by
a single company GUID; the board page server-renders its whole posting list into
a `window.pageData` JSON blob:

    list:   https://recruiting.paylocity.com/recruiting/jobs/All/{token}
            -> window.pageData = {"Jobs": [...], "Locations": [...], ...}
    detail: https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}
            -> schema.org JobPosting JSON-LD (the real description + employment type)

Accepts in company.career_url:
    c47e27a2-5dd2-408a-9ef0-c799cbdd5796
    https://recruiting.paylocity.com/recruiting/jobs/All/c47e27a2-.../ALL-JOBS

POSTED DATE
-----------
The list carries `PublishedDate`, which is the posting's ORIGINAL publish
timestamp (verified: boards return a spread of dates, and back-dated postings
keep their old date while newly posted ones show today). It is NOT a "last
updated" field, so it is safe to stamp straight into posted_at. When it is
missing/unparseable we leave posted_at=None rather than inventing a crawl-time
date — a fake "posted today" is worse than no date at all.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = "https://recruiting.paylocity.com/recruiting/jobs/All/{token}"
DETAIL = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"

# The board page embeds its data as `window.pageData = {...};`. We locate the
# opening brace and let the JSON decoder find the matching close — a regex for
# the whole object breaks on the HTML/JS that follows it.
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*")
_LD_JSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def extract_token(career_url: str) -> str:
    """Return the board GUID from a bare token or any recruiting.paylocity.com URL."""
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = _GUID.search(s)
    return m.group(0) if m else ""


def _page_data(html: str) -> Dict[str, Any]:
    m = _PAGE_DATA.search(html)
    if not m:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


class PaylocityCrawler(BaseCrawler):
    source_name = "paylocity"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "paylocity" or "recruiting.paylocity.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = _page_data(self._get(LIST.format(token=token)).text)
        # `IsInternal` postings are employee-only and are not publicly applyable.
        return [j for j in (data.get("Jobs") or []) if not j.get("IsInternal")]

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("JobTitle") or "").strip()
        loc = raw.get("JobLocation") or {}
        location = ", ".join(p for p in [loc.get("City"), loc.get("State")] if p)
        if not location:
            location = (raw.get("LocationName") or "").strip()
        if raw.get("IsRemote"):
            location = (location + " (Remote)").strip()

        job_id = raw.get("JobId")
        job_url = DETAIL.format(job_id=job_id) if job_id else ""
        dept = raw.get("HiringDepartment") or ""
        # The list API has no description; enrich_posted_date replaces this
        # placeholder with the real JD from the detail page's JSON-LD.
        description = " · ".join(p for p in [title, dept, location] if p)

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",  # filled by enrich_posted_date (JSON-LD employmentType)
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=parse_date(raw.get("PublishedDate")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """Pull the real JD + employment type from the per-job detail page.

        Called by the pipeline for NEW jobs only (post-dedupe), so the extra
        request stays cheap in steady state. posted_at is already correct from
        the list `PublishedDate`; we only re-stamp it if the detail JSON-LD
        disagrees, and we never overwrite a good date with nothing.
        """
        if not job.job_url:
            return job
        try:
            html = self._get(job.job_url).text
        except Exception:  # noqa: BLE001 - keep the list values on any failure
            return job

        posting = None
        for block in _LD_JSON.findall(html):
            try:
                obj = json.loads(block)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                posting = obj
                break
        if not posting:
            return job

        desc = posting.get("description") or ""
        if desc:
            job.description = truncate(clean_html(desc))
        emp = posting.get("employmentType") or ""
        if isinstance(emp, list):
            emp = ", ".join(str(e) for e in emp)
        if emp:
            job.employment_type = str(emp).strip()
        # datePosted is schema.org's publish date, same value as PublishedDate.
        posted = parse_date(posting.get("datePosted"))
        if posted is not None:
            job.posted_at = posted
        return job
