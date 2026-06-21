"""
crawlers/remoteok.py
--------------------
RemoteOK (remoteok.com) public remote-jobs API — no key, no auth:

    list: https://remoteok.com/api

This is a SENTINEL / AGGREGATOR source (like Himalayas / Remotive / HN hiring):
it lives as a SINGLE company row in the DB (name="RemoteOK", career_url="remoteok",
ats_type="remoteok", priority=low). fetch_jobs IGNORES company.career_url entirely
and hits the public API. Each payload job carries its OWN employer, so normalize_job
sets company_name from the PARSED employer (the `company` field) — NOT company.name
— and the dedupe hash is built on employer + url so two different employers (or two
postings) can't collide.

API SHAPE (curl-verified 2026-06-19, with a browser-like User-Agent):
    The endpoint returns a JSON ARRAY. The FIRST element is a legal/metadata
    object {last_updated, legal} — it is NOT a job and MUST be skipped.
    Each subsequent element (a job) has:
        id, slug, company, company_logo, position, tags:[str],
        location:<free text, e.g. "New York, New York, United States" / "Remote"
                  / "" / "London, ..., United Kingdom">,
        date:<ISO 8601 "2026-06-18T18:16:00+00:00">, epoch:<int seconds>,
        salary_min, salary_max, description:<html>, url, apply_url

RemoteOK rejects the default requests User-Agent (returns a Cloudflare/anti-bot
page or 403), so fetch_jobs sends a browser-like User-Agent header on the request.

US FILTER: `location` is a free-text city/state/country chain. RemoteOK is a
remote-jobs board, so an EMPTY location (or a bare "Remote"/"Anywhere") means a
truly worldwide-remote role — US-friendly. Otherwise we keep a job if it names
the US / North America / a worldwide bucket, OR names a US state (caught by a
whole-word state match so "New York, ..., United States" and bare "Colorado, "
both qualify while foreign chains ending in "United Kingdom"/"India"/"Brasil"
are dropped). This is conservative: a bare foreign city with no country/state
(e.g. "Plano, ") is dropped rather than risk a false positive.

description is HTML -> clean_html + truncate. posted_at comes from `date`
(ISO 8601) with `epoch` (seconds) as a fallback; parse_date handles both. No
detail call is needed — the list payload already has description + date — so
there is NO enrich_posted_date.

NOTE (legal/etiquette): RemoteOK's API terms ask callers to link back to the
RemoteOK job URL and credit RemoteOK as the source — which we do (job_url =
payload url, which points at the remoteok.com listing).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://remoteok.com/api"

# RemoteOK blocks the default requests UA; a browser-like UA gets the real JSON.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Substrings (lowercased) that mark a location as US-relevant / US-friendly.
# Worldwide/anywhere/remote roles are US-friendly by definition on a remote board.
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

# US state + DC names. Matched as whole words so "New York, ..., United States"
# and a bare "Colorado, " both qualify, while foreign chains (ending in
# "United Kingdom" / "India" / "Brasil" / etc.) are dropped.
US_STATES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
)


def _is_us_relevant(location: str) -> bool:
    """True if the job is open to US-based applicants.

    Empty string == truly worldwide-remote (US-friendly). Otherwise keep it if
    the location names the US / North America / a worldwide bucket, or names a
    US state. Conservative: a bare foreign city with no country/state token is
    dropped to avoid false positives.
    """
    s = (location or "").strip().lower()
    if not s:
        return True
    if any(tok in s for tok in US_TOKENS):
        return True
    return any(
        re.search(r"(?<![a-z])" + re.escape(state) + r"(?![a-z])", s)
        for state in US_STATES
    )


class RemoteOKCrawler(BaseCrawler):
    source_name = "remoteok"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "remoteok" or "remoteok.com" in s or "remoteok.io" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Hit the public API. Ignores company.career_url (sentinel source).

        The response is a JSON array whose FIRST element is a legal/metadata
        object — we skip it. Keeps only US-relevant postings (_is_us_relevant).
        """
        # RemoteOK blocks the default UA; send a browser-like UA for this call.
        data = self._get(API, headers={"User-Agent": _BROWSER_UA}).json()
        if not isinstance(data, list) or len(data) < 2:
            return []

        out: List[Dict[str, Any]] = []
        for raw in data[1:]:  # element 0 is the legal/metadata object — skip it
            if not isinstance(raw, dict):
                continue
            if _is_us_relevant(raw.get("location")):
                out.append(raw)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = (raw.get("company") or "").strip()
        title = (raw.get("position") or "").strip()
        location = (raw.get("location") or "").strip()
        job_url = (raw.get("url") or raw.get("apply_url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        # date is ISO 8601; epoch is seconds — either works with parse_date.
        posted_at = parse_date(raw.get("date")) or parse_date(raw.get("epoch"))

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,            # PARSED employer, not company.name
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, job_url),
        )
