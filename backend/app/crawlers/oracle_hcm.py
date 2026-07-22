"""
crawlers/oracle_hcm.py
----------------------
Oracle Cloud HCM / Fusion Recruiting ("ORC", the Candidate Experience career
sites that replaced Taleo). The candidate-facing REST API is public — no key,
no OAuth — but like Workday there is no global token: each customer has its own
Fusion pod hostname plus a site number.

    list:   https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
                ?onlyData=true&expand=requisitionList.workLocation
                &finder=findReqs;siteNumber={site},limit=200,offset=N
            -> items[0].requisitionList[] + items[0].TotalJobsCount
    detail: https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
                ?expand=all&onlyData=true&finder=ById;Id="{id}",siteNumber={site}
            -> ExternalDescriptionStr (the real JD) + ExternalPostedStartDate

Accepts in company.career_url:
    https://hcgn.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/
    hcgn.fa.us2.oraclecloud.com|CX_1
    hcgn.fa.us2.oraclecloud.com          (site defaults to CX_1)

POSTED DATE
-----------
`PostedDate` on the list is the requisition's EXTERNAL POSTING date (the day it
went live on the career site), not a last-modified stamp; the detail endpoint's
`ExternalPostedStartDate` is the same value with a time component, which is why
enrich_posted_date prefers it. Oracle also exposes `ExternalPostedEndDate` and
various profile version numbers — deliberately unused, since neither is a
publish date. If neither posting date parses we leave posted_at=None rather
than falling back to crawl time.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList.workLocation"
    "&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
    "sortBy=POSTING_DATES_DESC"
)
DETAIL = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
    "?expand=all&onlyData=true&finder=ById;Id={qid},siteNumber={site}"
)
JOB_PAGE = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"

PAGE = 200
MAX_PAGES = 10  # cap ~2000 requisitions per tenant
DEFAULT_SITE = "CX_1"

_SITE_URL = re.compile(
    r"(?:https?://)?([a-z0-9.-]+\.oraclecloud\.com)(?:/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+))?",
    re.I,
)
_JOB_URL = re.compile(
    r"https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/([^/?#]+)",
    re.I,
)


def parse_career_url(career_url: str) -> Optional[Tuple[str, str]]:
    """Return (host, site_number) from a career-site URL or a 'host|site' string."""
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return None
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 2 and parts[0]:
            return parts[0].lower(), parts[1] or DEFAULT_SITE
        return None
    m = _SITE_URL.search(s)
    if m:
        return m.group(1).lower(), (m.group(2) or DEFAULT_SITE)
    return None


def _location(raw: Dict[str, Any]) -> str:
    """Prefer the structured work location; fall back to Oracle's display string."""
    work = raw.get("workLocation") or []
    if work:
        first = work[0] or {}
        parts = [p for p in [first.get("TownOrCity"), first.get("Region2"),
                             first.get("Country")] if p]
        if parts:
            return ", ".join(parts)
    return (raw.get("PrimaryLocation") or "").strip()


class OracleHCMCrawler(BaseCrawler):
    source_name = "oracle_hcm"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("oracle_hcm", "oracle", "orc") or "oraclecloud.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        parsed = parse_career_url(company.career_url)
        if not parsed:
            return []
        host, site = parsed
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = self._get(LIST.format(host=host, site=site, limit=PAGE,
                                         offset=offset),
                             headers={"Accept": "application/json"}).json()
            items = data.get("items") or []
            if not items:
                break
            # The API wraps everything in a single "search result" item; the
            # postings live under its requisitionList.
            result = items[0] or {}
            batch = result.get("requisitionList") or []
            out.extend(batch)
            total = result.get("TotalJobsCount", 0) or 0
            offset += PAGE
            if not batch or offset >= total:
                break

        for j in out:
            j["_host"] = host
            j["_site"] = site
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("Title") or "").strip()
        location = _location(raw)
        host, site = raw.get("_host", ""), raw.get("_site", "")
        jid = raw.get("Id") or ""
        job_url = JOB_PAGE.format(host=host, site=site, jid=jid) if host and jid else ""

        workplace = (raw.get("WorkplaceType") or "").strip()
        if workplace.lower().startswith("remote"):
            location = (location + " (Remote)").strip()

        # The list carries at most a short teaser; enrich_posted_date swaps in the
        # full ExternalDescriptionStr from the detail endpoint.
        short = clean_html(raw.get("ShortDescriptionStr") or "")
        category = raw.get("Category") or ""
        description = truncate(short) if short else " · ".join(
            p for p in [title, category, location] if p)

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=(raw.get("JobSchedule") or "").strip(),
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=parse_date(raw.get("PostedDate")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """Fill the real JD (and a precise posting timestamp) from the detail
        endpoint. Called by the pipeline for NEW jobs only (post-dedupe).

        On any failure the job keeps its list-derived values, so a transient
        detail error never loses a live requisition.
        """
        m = _JOB_URL.search(job.job_url or "")
        if not m:
            return job
        host, site, jid = m.group(1), m.group(2), m.group(3)
        # Oracle's ById finder requires the id as a QUOTED string literal.
        qid = quote(f'"{jid}"', safe="")
        try:
            data = self._get(DETAIL.format(host=host, qid=qid, site=site),
                             headers={"Accept": "application/json"}).json()
        except Exception:  # noqa: BLE001 - keep the list values on failure
            return job
        items = data.get("items") or []
        if not items:
            return job
        detail = items[0] or {}

        # Stitch the candidate-facing sections; the corporate boilerplate is last
        # because truncate() keeps the head and only rescues visa disclosures.
        parts = [detail.get("ExternalDescriptionStr") or "",
                 detail.get("ExternalResponsibilitiesStr") or "",
                 detail.get("ExternalQualificationsStr") or "",
                 detail.get("CorporateDescriptionStr") or ""]
        body = clean_html("\n".join(p for p in parts if p))
        if body:
            job.description = truncate(body)

        # ExternalPostedStartDate is the same publish date as the list's
        # PostedDate but with a time, so it only ever refines what we have.
        posted = parse_date(detail.get("ExternalPostedStartDate"))
        if posted is not None:
            job.posted_at = posted
        return job
