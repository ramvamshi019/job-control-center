"""
crawlers/remotive.py
--------------------
Remotive (remotive.com) free public remote-jobs API — no key, no auth:

    list: https://remotive.com/api/remote-jobs
    (optional) per category: https://remotive.com/api/remote-jobs?category=software-dev&limit=...

This is a SENTINEL / AGGREGATOR source (like Himalayas / HN hiring): it lives as
a SINGLE company row in the DB (name="Remotive Remote", career_url="remotive",
ats_type="remotive"). fetch_jobs IGNORES company.career_url entirely and hits the
public API. Each payload job carries its OWN employer, so normalize_job sets
company_name from the PARSED employer (company_name field) — NOT company.name —
and the dedupe hash is built on employer + title + location + url so two
different employers can't collide.

API SHAPE (curl-verified 2026-06-19):
    top level: {00-warning, 0-legal-notice, job-count, total-job-count, jobs:[...]}
    job:       id, url, title, company_name, company_logo, category, tags:[str],
               job_type, publication_date:<ISO 8601 "2026-06-16T10:16:11">,
               candidate_required_location:<str e.g. "USA" / "Worldwide" /
               "Americas, Europe, Israel">, salary, description:<html>,
               company_logo_url

We crawl the tech-relevant categories (software-dev, data, devops-sysadmin) so a
single run pulls software AND data jobs. The `category` param filters the `jobs`
array server-side. `limit` raises the page size (the feed already returns the
full current batch, so limit is a safety belt). Results are deduped by job id
across categories before normalization.

US FILTER: candidate_required_location is a free-text country/region string. We
keep a job if it is US-relevant: empty (truly anywhere-remote), or it names the
US / North America / Americas / a worldwide/anywhere/global bucket. Country-locked
non-US listings (e.g. "Germany", "Brazil") are dropped.

description is HTML -> clean_html + truncate. posted_at comes from
publication_date (ISO 8601; parse_date handles it). No detail call is needed —
the list payload already has description + date — so there is NO
enrich_posted_date.

NOTE (legal/etiquette): Remotive's API legal-notice asks callers NOT to hammer
the endpoint (max ~4x/day is plenty; data is delayed 24h and changes slowly) and
to link back to the remotive.com job URL — which we do (job_url = payload url).
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://remotive.com/api/remote-jobs?category={category}&limit={limit}"
PAGE_LIMIT = 200  # safety belt; the feed returns the full current batch anyway

# Tech-relevant categories so one run pulls software + data + infra jobs.
CATEGORIES = ("software-dev", "data", "devops-sysadmin")

# Substrings (lowercased) that mark candidate_required_location as US-relevant /
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
)


def _is_us_relevant(location: str) -> bool:
    """True if the job is open to US-based applicants.

    Empty string == truly anywhere-remote (US-friendly). Otherwise keep it only
    if the location names the US / North America / a worldwide bucket. Note: a
    bare "remote" token is intentionally NOT treated as US-friendly here because
    Remotive's location field already names concrete countries/regions, so a
    country-locked listing (e.g. "Germany") should be dropped.
    """
    loc = (location or "").strip().lower()
    if not loc:
        return True
    return any(tok in loc for tok in US_TOKENS)


class RemotiveCrawler(BaseCrawler):
    source_name = "remotive"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "remotive" or "remotive.com" in s or "remotive.io" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Hit the public API per tech category. Ignores company.career_url.

        Dedupes jobs by id across categories and keeps only US-relevant
        postings (see _is_us_relevant).
        """
        seen_ids: set = set()
        out: List[Dict[str, Any]] = []
        for category in CATEGORIES:
            data = self._get(API.format(category=category, limit=PAGE_LIMIT)).json()
            jobs = data.get("jobs", []) or []
            for raw in jobs:
                job_id = raw.get("id")
                if job_id is not None and job_id in seen_ids:
                    continue
                if job_id is not None:
                    seen_ids.add(job_id)
                if _is_us_relevant(raw.get("candidate_required_location")):
                    out.append(raw)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = (raw.get("company_name") or "").strip()
        title = (raw.get("title") or "").strip()
        location = (raw.get("candidate_required_location") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        posted_at = parse_date(raw.get("publication_date"))
        employment_type = (raw.get("job_type") or "").strip()

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,            # PARSED employer, not company.name
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
