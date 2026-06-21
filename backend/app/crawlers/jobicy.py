"""
crawlers/jobicy.py
------------------
Jobicy (jobicy.com) public remote-jobs API — no key, no auth:

    list: https://jobicy.com/api/v2/remote-jobs?count=100
    (optional) &tag=<topic>   server-side topic filter (e.g. tag=data)
    (optional) &geo=<region>  server-side region filter (e.g. geo=usa)

This is a SENTINEL / AGGREGATOR source (like Himalayas / Remotive / HN hiring):
it lives as a SINGLE company row in the DB (name="Jobicy Remote",
career_url="jobicy", ats_type="jobicy"). fetch_jobs IGNORES company.career_url
entirely and hits the public API. Each payload job carries its OWN employer, so
normalize_job sets company_name from the PARSED employer (companyName) — NOT
company.name — and the dedupe hash is built on employer + title + location + url
so two different employers can't collide.

API SHAPE (curl-verified 2026-06-19):
    top level: {apiVersion, documentationUrl, friendlyNotice, jobCount,
                xRayCache, clientKey, lastUpdate, appliedFilters, jobs:[...],
                success}
    job:       id, url, jobSlug, jobTitle, companyName, companyLogo,
               jobIndustry:[str], jobType:[str e.g. "Full-Time"],
               jobGeo:<str e.g. "USA" / "EMEA,  LATAM,  Canada,  USA" /
               "Brazil">, jobLevel, jobExcerpt, jobDescription:<html>,
               pubDate:<ISO 8601 "2026-06-19T13:36:45+00:00">, salaryMin,
               salaryCurrency, salaryPeriod

We DELIBERATELY do NOT pass a server-side geo filter: jobGeo is a free-text,
comma-joined list of regions (e.g. "EMEA,  LATAM,  Canada,  USA"), and the
`geo=usa` server filter would miss/normalize those multi-region listings. Instead
we pull broadly across tech-relevant tags + a general feed, dedupe by job id, and
apply the same US-token client filter the other sentinels use. (Note: passing
geo=Anywhere is explicitly rejected by the API with success=false — anywhere/
worldwide listings come through the unfiltered feed and are kept client-side.)

US FILTER: jobGeo is a free-text country/region string. We keep a job if it is
US-relevant: empty (treated as anywhere-remote), or it names the US / North
America / Americas / a worldwide/anywhere/global bucket. Country-locked non-US
listings (e.g. "Brazil", "Spain", "UK", "EMEA, Poland") are dropped.

description is HTML -> clean_html + truncate. posted_at comes from pubDate
(ISO 8601; parse_date handles it). No detail call is needed — the list payload
already has description + date — so there is NO enrich_posted_date.

NOTE (legal/etiquette): Jobicy's friendlyNotice asks callers to credit Jobicy as
the source, redirect users to jobicy.com to apply (job_url = payload url, which
is the jobicy.com listing), and to fetch the feed only a few times per day —
excessive requests may be throttled. A 24/7 crawler with short posted-date
retention fits that just fine.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://jobicy.com/api/v2/remote-jobs?count={count}"
COUNT = 100  # API max page size; the feed returns the newest `count` jobs

# Tech-relevant topic tags so one run pulls data + engineering listings, plus a
# tagless general pull to catch US-remote roles the tags miss. Deduped by id.
TAGS = ("data", "engineering", "")

# Substrings (lowercased) that mark jobGeo as US-relevant / US-friendly.
# Worldwide/anywhere remote roles are US-friendly by definition.
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


def _is_us_relevant(geo: str) -> bool:
    """True if the job is open to US-based applicants.

    Empty string == treated as anywhere-remote (US-friendly). Otherwise keep it
    only if jobGeo names the US / North America / a worldwide bucket. jobGeo is a
    comma-joined region list (e.g. "EMEA,  LATAM,  Canada,  USA"), so a simple
    substring check over the whole string correctly keeps multi-region listings
    that include the US and drops country-locked non-US ones (e.g. "Brazil").
    Note: a bare "remote" token is intentionally NOT US-friendly here because
    jobGeo always names concrete countries/regions.
    """
    g = (geo or "").strip().lower()
    if not g:
        return True
    return any(tok in g for tok in US_TOKENS)


class JobicyCrawler(BaseCrawler):
    source_name = "jobicy"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "jobicy" or "jobicy.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Hit the public API across tech tags + a general pull. Ignores
        company.career_url.

        Dedupes jobs by id across tags and keeps only US-relevant postings
        (see _is_us_relevant).
        """
        seen_ids: set = set()
        out: List[Dict[str, Any]] = []
        for tag in TAGS:
            url = API.format(count=COUNT)
            if tag:
                url += "&tag={}".format(tag)
            data = self._get(url).json()
            jobs = data.get("jobs", []) or []
            for raw in jobs:
                job_id = raw.get("id")
                if job_id is not None and job_id in seen_ids:
                    continue
                if job_id is not None:
                    seen_ids.add(job_id)
                if _is_us_relevant(raw.get("jobGeo")):
                    out.append(raw)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = (raw.get("companyName") or "").strip()
        title = (raw.get("jobTitle") or "").strip()
        location = (raw.get("jobGeo") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("jobDescription") or ""))
        posted_at = parse_date(raw.get("pubDate"))

        # jobType is a list (e.g. ["Full-Time"]); join into a flat string.
        job_type = raw.get("jobType")
        if isinstance(job_type, (list, tuple)):
            employment_type = ", ".join(str(x).strip() for x in job_type if x)
        else:
            employment_type = (str(job_type).strip() if job_type else "")

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
