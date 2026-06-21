"""
crawlers/yc_waas.py
-------------------
Y Combinator "Work at a Startup" (WaaS) jobs — a SENTINEL / aggregator source
(ONE DB row -> many jobs across many YC startups). YC roles are early-stage,
high-signal and frequently visa-friendly, so this is a strong source-first feed
for an OPT job search.

WHY THIS PATH (auth investigation, curl-verified 2026-06-19):
    * workatastartup.com itself bot-walls anonymous requests (HTTP 406 on every
      path, even with a browser UA) and its Algolia job search needs a login —
      ycombinator.com/jobs ships `window.AlgoliaOpts = {"indices_not_set":true}`
      to anonymous users, i.e. NO public search credentials. So the global WaaS
      job search is NOT usable without auth.
    * BUT two PUBLIC, no-auth, no-key surfaces exist and together give real US
      jobs:
        1. The yc-oss open dataset (a mirror of YC's public company directory):
               https://yc-oss.github.io/api/companies/all.json
           ~6k companies, each with `slug`, `isHiring`, `status`, `regions`.
           This is our COMPANY FEEDER — we keep Active companies flagged
           isHiring whose regions look US-relevant.
        2. Each company's public jobs page on ycombinator.com:
               https://www.ycombinator.com/companies/{slug}/jobs
           It is an Inertia.js page: the server embeds the page props as JSON in
           a `data-page="..."` attribute. props.jobPostings is the real list of
           live roles (verified `signedIn:false`, no cookie needed). Each posting
           carries title, url, location, type, role, visa, skills, salaryRange,
           companyName, createdAt.
    So: one sentinel row -> fetch the public company list -> fetch each hiring
    company's public jobs page -> parse postings. company_name is the PARSED
    employer (jobPosting.companyName), NOT company.name.

US FILTER: applied PER JOB on the posting `location` (e.g. "United States -
Remote / Remote (US)", "Deerfield, MA, US / Remote (US)", "UK / Remote (US)").
We keep a job if the location names the US / North America or is a worldwide /
"remote (us)" bucket; pure non-US locations (e.g. "London, UK") are dropped.
Note: many WaaS roles append "/ Remote (US)" meaning they are open to US-remote,
so those are kept even when the primary office is abroad. We do NOT filter on the
`visa` field — F-1/OPT holders have valid US work authorization, and "US
citizen/visa only" is the common WaaS default that still includes visa holders.

POSTED DATE: WaaS exposes only a COARSE relative age ("3 months", "9 months",
"over 1 year") — there is no exact timestamp anywhere on the public list OR
detail page. We approximate posted_at from that bucket (see _approx_posted_at)
so the retention/scoring engines have a value, but it is necessarily fuzzy.

enrich_posted_date(): the LIST page has no description. For NEW jobs only
(post-dedupe), we fetch the public job DETAIL page
(ycombinator.com{job.url}) which is the same Inertia shape and adds
props.job.description, and use it to fill description (and refresh the approx
posted_at). No exact date is available there either.

BOUNDING: there are ~1.5k hiring YC companies. To stay polite and bounded (like
himalayas' MAX_PAGES cap) we cap at MAX_COMPANIES per run. The yc-oss list is
stable-ordered, so this walks the same prefix each run; that is acceptable for a
24/7 crawler that re-runs frequently. Bump MAX_COMPANIES to widen coverage.
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

# Public YC company directory mirror (no auth, no key).
COMPANY_LIST = "https://yc-oss.github.io/api/companies/all.json"
# Public per-company jobs page (Inertia props embedded in data-page=).
JOBS_PAGE = "https://www.ycombinator.com/companies/{slug}/jobs"
SITE = "https://www.ycombinator.com"

# Politeness / bounding. ~1.5k hiring cos; cap how many we hit per run.
MAX_COMPANIES = 400

# Pull the Inertia page-props JSON out of the server-rendered HTML.
_DATA_PAGE_RE = re.compile(r'data-page="(.*?)"\s*>', re.S)

# Substrings (lowercased) that mark a posting location as US-relevant /
# US-friendly. WaaS frequently tags US-remote roles "Remote (US)".
US_TOKENS = (
    "united states",
    " us ",
    "/ us",
    ", us",
    "us /",
    "(us)",
    "u.s.",
    "usa",
    "north america",
    "america / canada",
    "americas",
    "remote (us)",
    "anywhere",
    "worldwide",
    "global",
)

# yc-oss region strings that count as US-relevant at the COMPANY level (a cheap
# pre-filter before we fetch the page; the real filter is per-job on location).
US_REGION_TOKENS = (
    "united states",
    "america",
    "remote",
)


def _parse_inertia_props(html_text: str) -> Optional[Dict[str, Any]]:
    """Decode the Inertia `data-page` JSON blob -> props dict, or None."""
    if not html_text:
        return None
    m = _DATA_PAGE_RE.search(html_text)
    if not m:
        return None
    try:
        data = json.loads(_html.unescape(m.group(1)))
    except (ValueError, TypeError):
        return None
    props = data.get("props")
    return props if isinstance(props, dict) else None


def _company_is_us_relevant(co: Dict[str, Any]) -> bool:
    blob = (
        " ".join(str(r) for r in (co.get("regions") or []))
        + " "
        + str(co.get("all_locations") or "")
    ).lower()
    return any(tok in blob for tok in US_REGION_TOKENS)


def _job_is_us_relevant(location: str) -> bool:
    loc = f" {(location or '').lower()} "
    if not loc.strip():
        # No location given -> treat as worldwide/remote (US-friendly default).
        return True
    return any(tok in loc for tok in US_TOKENS)


# Map WaaS' coarse relative-age buckets to an approximate age in days. WaaS
# never exposes an exact timestamp, so this is intentionally fuzzy.
_REL_UNIT_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def _approx_posted_at(created_at: str) -> Optional[datetime]:
    """Best-effort posted_at from strings like '3 months', '9 months',
    'over 1 year', 'about 1 month', '5 days'. Returns None if unparseable."""
    if not created_at:
        return None
    s = str(created_at).lower().strip()
    if "less than" in s or "just now" in s:
        return datetime.utcnow()
    m = re.search(r"(\d+)\s*(day|week|month|year)", s)
    if not m:
        # "a month", "an hour", "over a year" etc. -> treat the unit as 1.
        m2 = re.search(r"\b(day|week|month|year)\b", s)
        if not m2:
            return None
        n, unit = 1, m2.group(1)
    else:
        n, unit = int(m.group(1)), m.group(2)
    days = n * _REL_UNIT_DAYS.get(unit, 0)
    if days <= 0:
        return None
    return datetime.utcnow() - timedelta(days=days)


class YCWaaSCrawler(BaseCrawler):
    source_name = "yc_waas"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return (
            s == "yc_waas"
            or "workatastartup" in s
            or "ycombinator.com/companies" in s
        )

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """SENTINEL: ignore company.career_url. Pull the public YC company list,
        keep Active + isHiring + US-relevant companies, then fetch each one's
        public jobs page and collect US-relevant postings.

        Each returned raw dict is a single jobPosting augmented with the parsed
        employer fields already present on the posting (companyName, etc.).
        Per-company page failures are swallowed so one bad slug can't stop the
        run (mirrors base.crawl()'s per-company resilience).
        """
        companies = self._get(COMPANY_LIST).json()
        if not isinstance(companies, list):
            return []

        hiring = [
            c
            for c in companies
            if isinstance(c, dict)
            and c.get("isHiring")
            and c.get("status") == "Active"
            and c.get("slug")
            and _company_is_us_relevant(c)
        ]

        out: List[Dict[str, Any]] = []
        for co in hiring[:MAX_COMPANIES]:
            slug = co["slug"]
            try:
                html_text = self._get(JOBS_PAGE.format(slug=slug)).text
            except Exception:  # noqa: BLE001 - skip a broken/404 company page
                continue
            props = _parse_inertia_props(html_text)
            if not props:
                continue
            postings = props.get("jobPostings") or []
            for jp in postings:
                if not isinstance(jp, dict):
                    continue
                if _job_is_us_relevant(jp.get("location") or ""):
                    out.append(jp)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: employer comes from the PARSED posting, not company.name.
        employer = (raw.get("companyName") or "").strip()
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        employment_type = (raw.get("type") or "").strip()

        # `url` is a site-relative path like /companies/<slug>/jobs/<id>-<slug>.
        url_path = (raw.get("url") or "").strip()
        job_url = (SITE + url_path) if url_path.startswith("/") else url_path

        # The list page has no description; enrich_posted_date fills it for NEW
        # jobs. Until then, synthesize a short blurb from the structured fields.
        bits = [
            raw.get("companyOneLiner"),
            f"Role: {raw.get('prettyRole')}" if raw.get("prettyRole") else None,
            f"Skills: {', '.join(raw.get('skills'))}" if raw.get("skills") else None,
            f"Salary: {raw.get('salaryRange')}" if raw.get("salaryRange") else None,
            f"Visa: {raw.get('visa')}" if raw.get("visa") else None,
        ]
        description = truncate(clean_html(" | ".join(b for b in bits if b)))

        posted_at = _approx_posted_at(raw.get("createdAt") or "")

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

    def enrich_posted_date(self, job: Job) -> Job:
        """Called for NEW jobs only (post-dedupe). The public job DETAIL page is
        the same Inertia shape and adds props.job.description; we use it to fill
        the real description. WaaS exposes no exact timestamp even here, so we
        only refresh the approximate posted_at from the detail's createdAt."""
        if not job.job_url:
            return job
        try:
            html_text = self._get(job.job_url).text
        except Exception:  # noqa: BLE001 - keep the list-level data on failure
            return job
        props = _parse_inertia_props(html_text)
        if not props:
            return job
        jp = props.get("job") or props.get("jobPosting")
        if not isinstance(jp, dict):
            return job

        desc = jp.get("description")
        if desc:
            job.description = truncate(clean_html(desc))
        approx = _approx_posted_at(jp.get("createdAt") or "")
        if approx is not None:
            job.posted_at = approx
        return job
