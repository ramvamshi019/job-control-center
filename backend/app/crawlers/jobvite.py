"""
crawlers/jobvite.py
-------------------
Jobvite ATS public career site (no key needed). Jobvite hosts employer-branded
career sites at jobs.jobvite.com/{token}; the job LIST lives at a single
server-rendered HTML page:

    list:   https://jobs.jobvite.com/{token}/jobs/positions
    detail: https://jobs.jobvite.com/{token}/job/{job_id}

`token` is the company's careersite slug, e.g. "sitecore" from
    https://jobs.jobvite.com/sitecore
We accept any of these in company.career_url:
    sitecore
    https://jobs.jobvite.com/sitecore
    https://jobs.jobvite.com/sitecore/jobs/positions
    https://jobs.jobvite.com/sitecore/job/oGm1zfwm   (per-job URL -> slug)

WHY NOT THE XML/JSON FEED: Jobvite's documented data feeds
(hire.jobvite.com/CompanyJobs/Xml.aspx?c=...&key=...&sc=...) REQUIRE a per-client
API key + secret, so they are off-limits for a generic no-auth crawler. The
public careersite HTML needs no auth and is the only no-key surface, so we parse
it. (curl-verified 2026-06-19 on sitecore + loandepot boards.)

LIST PAGE SHAPE (HTML, curl-verified): jobs are grouped under <h3>Category</h3>
headers and rendered as rows:
    <li class="row">
      <a href="/{token}/job/{id}" class="flex-row">
        <div class="jv-job-list-name"> Title </div>
        <div class="ml-auto jv-job-list-location"> City, Region </div>
      </a>
    </li>
The list page has NO posted date and NO description, so we stamp those from the
per-job detail page in enrich_posted_date(), which the pipeline calls for NEW
jobs only (post-dedupe) — same light-live-path pattern as Rippling/Gem/BambooHR.

DETAIL PAGE SHAPE: each job page embeds a JSON-LD <script type="application/ld+json">
JobPosting object with datePosted (ISO date), employmentType, and an HTML
description. We pull date + description from there.

US FILTER: this is a per-company crawler, so a single token may be a US or a
non-US employer. We keep a row if its location is US-relevant (names a US state /
"United States"/"USA", or is remote/worldwide/anywhere) OR is ambiguous (empty,
or a "N Locations" multi-site badge that may include US sites). We drop only rows
whose location clearly names a non-US country, so we never lose a real US posting
to a vague location string.

Jobvite skews to mid/large US employers (loanDepot, Samtec, ActioNet, ...),
exactly the entry/mid data-engineer segment, and is cross-probeable by token like
Rippling/Gem. seeding='cross-probe tokens' (token = the jobs.jobvite.com slug).
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = "https://jobs.jobvite.com/{token}/jobs/positions"
JOB_URL = "https://jobs.jobvite.com/{token}/job/{jid}"

# One job row: anchor (-> token + id), title, location. DOTALL because the
# location cell spans several lines on the live page.
ROW_RE = re.compile(
    r'<a\s+href="/([^/"]+)/job/([A-Za-z0-9]+)"[^>]*>\s*'
    r'<div class="jv-job-list-name">\s*(.*?)\s*</div>\s*'
    r'<div class="[^"]*jv-job-list-location">\s*(.*?)\s*</div>',
    re.DOTALL | re.IGNORECASE,
)

# JSON-LD JobPosting block on the detail page.
LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# US state names + 2-letter abbreviations, for the location filter.
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington dc", "washington d.c.", "puerto rico",
}
_US_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc", "pr",
}
_US_NAMES = {"united states", "usa", "u.s.", "u.s.a.", "us", "america"}
_REMOTE_OK = {"remote", "worldwide", "anywhere", "global", "virtual"}


def extract_token(career_url: str) -> str:
    """Careersite slug from a jobs.jobvite.com URL or a bare slug.

    jobs.jobvite.com/sitecore                  -> sitecore
    jobs.jobvite.com/sitecore/jobs/positions   -> sitecore
    jobs.jobvite.com/sitecore/job/oGm1zfwm     -> sitecore  (per-job URL)
    sitecore                                   -> sitecore
    """
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"jobs\.jobvite\.com/([^/?#]+)", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s  # already a bare slug
    return s.split("/")[-1]


def _is_us_relevant(location: str) -> bool:
    """True if a list-row location should be kept for a US job search.

    Keep when US-relevant (US state / United States / remote-worldwide) OR
    ambiguous (empty, or an "N Locations" multi-site badge). Drop only when the
    string clearly names somewhere and that somewhere isn't US/remote.
    """
    loc = (location or "").strip().lower()
    if not loc:
        return True  # no location given -> keep, don't risk dropping a US role
    # "2 Locations", "6 Locations" multi-site badge -> ambiguous, keep.
    if re.fullmatch(r"\d+\s+locations?", loc):
        return True
    tokens = {t.strip(" .,") for t in re.split(r"[,/|;]| - ", loc) if t.strip(" .,")}
    if any(w in loc for w in _REMOTE_OK):
        return True
    if any(n in loc for n in _US_NAMES):
        return True
    if tokens & _US_STATES:
        return True
    if tokens & _US_ABBR:
        return True
    return False


def _clean(fragment: str) -> str:
    """Strip tags/entities and collapse whitespace from an HTML fragment."""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip().strip(",").strip()


class JobviteCrawler(BaseCrawler):
    source_name = "jobvite"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "jobvite" or "jobs.jobvite.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        html = self._get(LIST.format(token=token)).text
        out: List[Dict[str, Any]] = []
        for m_token, jid, name, loc in ROW_RE.findall(html):
            location = _clean(loc)
            if not _is_us_relevant(location):
                continue
            out.append(
                {
                    "token": m_token or token,
                    "jid": jid,
                    "title": _clean(name),
                    "location": location,
                }
            )
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        token = raw.get("token") or extract_token(company.career_url)
        jid = raw.get("jid") or ""
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        job_url = JOB_URL.format(token=token, jid=jid) if jid else ""

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",  # filled by enrich_posted_date (detail JSON-LD)
            job_url=job_url,
            source=self.source_name,
            description="",       # filled by enrich_posted_date (detail JSON-LD)
            posted_at=None,       # filled by enrich_posted_date (datePosted)
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """List page has no date/description — pull them from the detail page's
        JSON-LD JobPosting. Called by the pipeline for NEW jobs only (post-dedupe),
        so it stays cheap. On failure the job keeps posted_at=None (kept, not
        pruned) so a transient detail error never loses a posting."""
        if not job.job_url:
            return job
        try:
            html = self._get(job.job_url).text
        except Exception:  # noqa: BLE001 - keep None on failure
            return job

        posting = self._parse_jobposting(html)
        if not posting:
            return job

        posted = parse_date(posting.get("datePosted"))
        if posted:
            job.posted_at = posted

        desc = posting.get("description") or ""
        if desc:
            job.description = truncate(clean_html(desc))

        et = posting.get("employmentType")
        if isinstance(et, list):
            et = ", ".join(str(x) for x in et if x)
        if et:
            job.employment_type = str(et)
        return job

    @staticmethod
    def _parse_jobposting(html: str) -> Dict[str, Any]:
        """Return the first JSON-LD object whose @type is JobPosting, or {}."""
        import json

        for block in LDJSON_RE.findall(html or ""):
            try:
                data = json.loads(block.strip())
            except Exception:  # noqa: BLE001 - skip malformed blocks
                continue
            candidates = data if isinstance(data, list) else [data]
            for d in candidates:
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    return d
        return {}
