"""
crawlers/new_grad.py
--------------------
Community GitHub new-grad / internship lists (SimplifyJobs) — no key, no auth.

These crowd-sourced repos track ENTRY-LEVEL US tech roles (new-grad full-time +
internships), are frequently visa-tagged, and surface postings the big indexers
(JobRight etc.) never catch — the single highest-value feed for an F-1
entry-level candidate.

RAW LISTINGS (curl-verified 2026-06-19):
    new-grad:   https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
    internship: https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json
Each is a JSON ARRAY of ~18k listing objects.

This is a SENTINEL / AGGREGATOR source (like himalayas / remotive): it lives as
a SINGLE company row in the DB (name="New Grad GitHub", career_url="new_grad",
ats_type="new_grad"). fetch_jobs IGNORES company.career_url entirely and GETs the
raw listings.json file(s). Each listing carries its OWN employer, so normalize_job
sets company_name from the PARSED listing (company_name) — NOT company.name — and
the dedupe hash is built on employer + title + location + url so two employers
can't collide and the same role at two cities stays distinct.

LISTING SHAPE (curl-verified 2026-06-19):
    source         e.g. "Simplify" / a GitHub username
    category       e.g. "Software" / "AI/ML/Data"
    company_name   employer (PARSE THIS — it is the real employer)
    title          role title
    active         bool   -> SKIP if false (role closed / filled)
    is_visible     bool   -> SKIP if false (hidden from the published table)
    date_posted    epoch seconds
    date_updated   epoch seconds
    url            apply link (the canonical job_url)
    locations      list[str] e.g. ["Dallas, TX"], ["Remote in USA"], ["NYC","SF"]
    sponsorship    "Offers Sponsorship" / "Does Not Offer Sponsorship" /
                   "U.S. Citizenship is Required" / "Other"
    degrees        list[str]
    terms          list[str] e.g. ["Summer 2026"]  (internship repo only)

US FILTER: locations is a list of human-written place strings (cities, state
abbrevs, full state names, city aliases like "NYC"/"SF"/"LA", "Remote in USA",
"United States", bare "Remote"). We KEEP a listing if ANY of its locations is
US-relevant and DROP country-locked non-US ones (UK / Canada / EMEA / worldwide-
only / etc.). "uk" etc. are matched as whole words so "Milwaukee, WI" isn't
mistaken for the UK.

No detail call is needed — the list payload already has employer + url + date —
so there is NO enrich_posted_date.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import make_hash, truncate

# Both raw listings files. The new-grad repo is primary; the Summer2026
# internship companion repo adds entry-level intern roles (also visa-tagged).
LISTINGS_URLS = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
)

# US state postal abbreviations (+ DC).
_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# Full US state names (lowercased).
_STATE_NAMES = {
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
}

# Common metro aliases the lists use without a state.
_CITY_ALIAS = {"nyc", "sf", "la", "d.c.", "dc", "silicon valley", "bay area"}

# US-naming phrases (substring match is safe — long & unambiguous).
_US_PHRASES = (
    "united states", "usa", "u.s.a", "u.s.",
    "remote in usa", "remote in us", "us remote", "remote - us",
)

# Country / region names that mark a location as NON-US. Matched as WHOLE WORDS
# (boundary-aware) so short codes like "uk" don't hit inside "Milwaukee".
_NON_US_WORDS = {
    "uk", "united kingdom", "canada", "ireland", "germany", "india",
    "singapore", "australia", "uae", "dubai", "spain", "france",
    "netherlands", "poland", "israel", "japan", "china", "mexico", "brazil",
    "scotland", "wales", "england", "remote globally", "remote worldwide",
    "worldwide", "emea", "apac",
}


def _has_word(low: str, word: str) -> bool:
    """True if `word` appears in `low` on alphabetic boundaries (case-folded)."""
    return re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low) is not None


def _loc_is_us(loc: str) -> bool:
    """Is a single location string US-relevant?"""
    low = (loc or "").lower().strip()
    if not low:
        return False
    # Reject anything that names a non-US country/region first.
    if any(_has_word(low, n) for n in _NON_US_WORDS):
        return False
    if low in ("remote", "remote in usa", "remote us"):
        return True  # bare "Remote" -> treat as US-remote on a US list
    if low in _CITY_ALIAS:
        return True
    if any(p in low for p in _US_PHRASES):
        return True
    if any(_has_word(low, sn) for sn in _STATE_NAMES):
        return True
    toks = [t.strip(".") for t in re.split(r"[\s,]+", low) if t]
    if any(t.upper() in _STATE_ABBR for t in toks):
        return True
    return False


def _is_us_relevant(locations: List[str]) -> bool:
    """Keep the listing if ANY of its locations is US-relevant."""
    locs = [str(x) for x in (locations or []) if x]
    if not locs:
        return False  # no location at all -> can't confirm US; drop
    return any(_loc_is_us(loc) for loc in locs)


class NewGradCrawler(BaseCrawler):
    source_name = "new_grad"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "new_grad" or "new-grad-positions" in s or "summer2026-internships" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """GET each raw listings.json and keep active+visible+US listings.

        Ignores company.career_url. One broken/unreachable file does not abort
        the others.
        """
        out: List[Dict[str, Any]] = []
        for url in LISTINGS_URLS:
            try:
                data = self._get(url).json()
            except Exception:  # noqa: BLE001 - skip an unreachable companion file
                continue
            if not isinstance(data, list):
                continue
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                if not raw.get("active"):
                    continue
                if not raw.get("is_visible"):
                    continue
                if not _is_us_relevant(raw.get("locations")):
                    continue
                out.append(raw)
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: employer comes from the PARSED listing, not company.name.
        employer = (raw.get("company_name") or "").strip()
        title = (raw.get("title") or "").strip()

        # Keep only the US-relevant locations in the displayed string.
        us_locs = [
            str(x).strip()
            for x in (raw.get("locations") or [])
            if x and _loc_is_us(str(x))
        ]
        location = ", ".join(us_locs) or ", ".join(
            str(x).strip() for x in (raw.get("locations") or []) if x
        )

        job_url = (raw.get("url") or "").strip()
        posted_at = parse_date(raw.get("date_posted") or raw.get("date_updated"))

        # Short description line: sponsorship stance + term(s) where present.
        bits: List[str] = []
        sponsorship = (raw.get("sponsorship") or "").strip()
        if sponsorship and sponsorship.lower() != "other":
            bits.append(f"Sponsorship: {sponsorship}")
        terms = [str(t).strip() for t in (raw.get("terms") or []) if t]
        if terms:
            bits.append("Term: " + ", ".join(terms))
        category = (raw.get("category") or "").strip()
        if category:
            bits.append(f"Category: {category}")
        description = truncate(" | ".join(bits))

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type="intern" if terms else "",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
