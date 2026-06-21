"""
crawlers/himalayas.py
---------------------
Himalayas (himalayas.app) public remote-jobs API — no key, no auth:

    list: https://himalayas.app/jobs/api?limit=20&offset=0

This is a SENTINEL / AGGREGATOR source (like Rippling): it lives as a SINGLE
company row in the DB (name="Himalayas Remote", career_url="himalayas",
ats_type="himalayas"). fetch_jobs IGNORES company.career_url entirely and pages
the public API. Each payload job carries its OWN employer, so normalize_job sets
company_name from the PARSED employer (companyName) — NOT company.name — and the
dedupe hash is built on employer + job_url so two different employers can't
collide.

API SHAPE (curl-verified 2026-06-19):
    top level: {comments, updatedAt, offset, limit, totalCount, jobs:[...]}
    job:       title, excerpt, companyName, companySlug, companyLogo,
               employmentType, locationRestrictions:[str], timezoneRestrictions,
               categories, description:<html>, pubDate:<epoch seconds>,
               expiryDate, applicationLink, guid

PAGINATION: the API HARD-CAPS the page size at 20 regardless of `limit`, so we
page with `offset` in steps of 20. The feed is sorted newest-first (pubDate
descending, verified), so paging walks back in time — exactly what a 24/7
crawler with short posted-date retention wants. We cap at MAX_PAGES pages.

US FILTER: locationRestrictions is a list of allowed countries. We keep a job if
it is US-relevant: empty list (truly worldwide-remote), or any restriction names
the US / North America / Americas / "anywhere"/"worldwide"/"global"/"remote".
Country-locked non-US listings (e.g. ["Germany"]) are dropped.

description is HTML -> clean_html + truncate. posted_at comes from pubDate
(epoch seconds; parse_date handles it). No detail call is needed — the list
payload already has description + date — so there is NO enrich_posted_date.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://himalayas.app/jobs/api?limit={limit}&offset={offset}"
PAGE_SIZE = 20      # API hard-caps at 20/page regardless of the limit param
MAX_PAGES = 50      # 50 pages * 20 = up to 1000 newest jobs per run

# Substrings (lowercased) that mark a location restriction as US-relevant /
# US-friendly. Worldwide/anywhere remote roles are US-friendly by definition.
US_TOKENS = (
    "united states",
    "usa",
    "u.s.",
    "us-",
    "north america",
    "americas",
    "anywhere",
    "worldwide",
    "global",
    "remote",
)


def _is_us_relevant(location_restrictions: List[str]) -> bool:
    """True if the job is open to US-based applicants.

    Empty list == truly worldwide remote (US-friendly). Otherwise keep it only
    if some restriction names the US / North America / a worldwide bucket.
    """
    lr = [str(x).lower() for x in (location_restrictions or []) if x]
    if not lr:
        return True
    return any(any(tok in loc for tok in US_TOKENS) for loc in lr)


class HimalayasCrawler(BaseCrawler):
    source_name = "himalayas"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "himalayas" or "himalayas.app" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Page the public API newest-first. Ignores company.career_url.

        Keeps only US-relevant postings (see _is_us_relevant). Stops early on a
        short/empty page so we don't hammer the API past the end of the feed.
        """
        out: List[Dict[str, Any]] = []
        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            data = self._get(API.format(limit=PAGE_SIZE, offset=offset)).json()
            jobs = data.get("jobs", []) or []
            if not jobs:
                break
            for raw in jobs:
                if _is_us_relevant(raw.get("locationRestrictions")):
                    out.append(raw)
            if len(jobs) < PAGE_SIZE:
                break  # reached the end of the feed
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = (raw.get("companyName") or "").strip()
        title = (raw.get("title") or "").strip()
        location = ", ".join(
            str(x).strip() for x in (raw.get("locationRestrictions") or []) if x
        )
        # applicationLink is the canonical himalayas listing; guid is identical
        # in practice but kept as a fallback.
        job_url = (raw.get("applicationLink") or raw.get("guid") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        posted_at = parse_date(raw.get("pubDate"))
        employment_type = (raw.get("employmentType") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
