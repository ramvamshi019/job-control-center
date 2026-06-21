"""
crawlers/breezy.py
------------------
Breezy HR public job board JSON (no key, no auth) — PER-COMPANY:

    list: https://{token}.breezy.hr/json

`token` is the company's Breezy subdomain. We accept any of these in
company.career_url:
    reveleer
    https://reveleer.breezy.hr
    https://reveleer.breezy.hr/json
    https://reveleer.breezy.hr/p/d447666d8ce4-some-job   (we still get "reveleer")

API SHAPE (curl-verified 2026-06-19 against reveleer.breezy.hr):
    top level: a JSON LIST of positions (NOT an object).
    position: {
        id, friendly_id, name,            # name == job title
        url,                              # canonical https://{token}.breezy.hr/p/...
        published_date:<ISO 8601 "2026-06-04T15:01:39.320Z">,
        type:   {id, name},               # employment type, e.g. {"id":"fullTime","name":"Full-Time"}
        location: {                       # primary location object
            country:{name,id}, state:{name,id}?, city?,
            is_remote:<bool>, remote_details?, name:<"United States" / "Columbus, OH">
        },
        department, salary,
        company:{name, logo_url, friendly_id, isMultipleLocationsEnabled},
        locations:[ ...same shape as location... ]   # all locations
    }

IMPORTANT: the LIST payload has NO description field (and ?descriptions=true does
NOT add it). The full description lives on the position page's JSON-LD
<script type="application/ld+json"> JobPosting block. We pull it lazily in
enrich_posted_date(), which the pipeline calls for NEW jobs ONLY (post-dedupe),
so a 24/7 crawler never re-fetches every position's HTML on every pass. The list
already carries the posted date + employment type + location, so the core crawl
works fully even if enrichment fails.

US FILTER (lenient): keep a job if it's US-relevant — country == US, a worldwide/
remote/anywhere bucket, or no resolvable country (we don't drop on ambiguity).
Concrete non-US country-locked listings (e.g. India / Chennai) are dropped.
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

API = "https://{token}.breezy.hr/json"

LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# Substrings (lowercased) that mark a location as US-relevant / US-friendly.
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


def extract_token(career_url: str) -> str:
    """Get the Breezy subdomain token from a URL or a bare token.

    "reveleer"                              -> "reveleer"
    "https://reveleer.breezy.hr"           -> "reveleer"
    "https://reveleer.breezy.hr/json"      -> "reveleer"
    "https://reveleer.breezy.hr/p/xyz-job" -> "reveleer"
    """
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"([A-Za-z0-9_-]+)\.breezy\.hr", s)
    if m:
        return m.group(1)
    # Bare token (no scheme, no dots, no path).
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _is_us_relevant(raw: Dict[str, Any]) -> bool:
    """True if the posting is open to US-based applicants.

    Lenient by design (per the task): keep US-country jobs, remote/worldwide
    buckets, and anything where we can't resolve a concrete country. Only drop
    when a concrete NON-US country is named and nothing US-friendly is present.
    """
    # Gather every location object on the posting.
    locs: List[Dict[str, Any]] = []
    if isinstance(raw.get("location"), dict):
        locs.append(raw["location"])
    for loc in raw.get("locations") or []:
        if isinstance(loc, dict):
            locs.append(loc)

    saw_concrete_country = False
    for loc in locs:
        country = (loc.get("country") or {}) if isinstance(loc.get("country"), dict) else {}
        cid = str(country.get("id") or "").strip().upper()
        cname = str(country.get("name") or "").strip().lower()
        name = str(loc.get("name") or "").strip().lower()

        if cid == "US":
            return True
        if loc.get("is_remote"):
            return True
        # Free-text name carrying a US/worldwide/remote token.
        if any(tok in name for tok in US_TOKENS):
            return True
        if cid or cname:
            saw_concrete_country = True

    # No location info at all -> don't drop on ambiguity (lenient).
    if not locs:
        return True
    # Some concrete non-US country was named and nothing US-friendly matched.
    if saw_concrete_country:
        return False
    return True


class BreezyCrawler(BaseCrawler):
    source_name = "breezy"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "breezy" or "breezy.hr" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get(API.format(token=token)).json()
        # Breezy returns a JSON list of positions directly.
        positions = data if isinstance(data, list) else []
        return [p for p in positions if _is_us_relevant(p)]

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("name") or "").strip()

        # Prefer the human "name" of the primary location ("United States" /
        # "Columbus, OH"); fall back to a remote tag or the country name.
        loc_obj = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        location = (loc_obj.get("name") or "").strip()
        if not location:
            if loc_obj.get("is_remote"):
                location = "Remote"
            else:
                country = loc_obj.get("country") or {}
                location = str(country.get("name") or "").strip()

        job_url = (raw.get("url") or "").strip()

        # type is an object {id, name}; "name" is the readable label.
        type_obj = raw.get("type") if isinstance(raw.get("type"), dict) else {}
        employment_type = (type_obj.get("name") or "").strip()

        posted_at = parse_date(raw.get("published_date"))

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,  # per-company source: use the DB company
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description="",  # filled by enrich_posted_date (detail JSON-LD)
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """The list JSON has no description; pull it from the position page's
        JSON-LD JobPosting. Called for NEW jobs only (post-dedupe), so it stays
        cheap. posted_at already came from the list (precise timestamp), so we
        only backfill it from the detail page when the list had none. On any
        failure the job is kept unchanged so a transient detail error never
        drops a posting."""
        if not job.job_url:
            return job
        try:
            html = self._get(job.job_url).text
        except Exception:  # noqa: BLE001 - keep existing values on failure
            return job

        posting = self._parse_jobposting(html)
        if not posting:
            return job

        desc = posting.get("description") or ""
        if desc:
            job.description = truncate(clean_html(desc))

        # Only fill posted_at from the detail page if the LIST didn't supply one.
        # The list's published_date is precise (full timestamp); the JSON-LD
        # datePosted is often a coarser/staler day-only value, so we don't let
        # it overwrite a good list date.
        if job.posted_at is None:
            posted = parse_date(posting.get("datePosted"))
            if posted:
                job.posted_at = posted

        if not job.employment_type:
            et = posting.get("employmentType")
            if isinstance(et, list):
                et = ", ".join(str(x) for x in et if x)
            if et:
                job.employment_type = str(et)
        return job

    @staticmethod
    def _parse_jobposting(html: str) -> Dict[str, Any]:
        """Return the first JSON-LD object whose @type is JobPosting, or {}."""
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
