"""
crawlers/themuse.py
-------------------
The Muse (themuse.com) public jobs API — no key, no auth:

    list: https://www.themuse.com/api/public/jobs?page={n}
    (optional) &category=Software%20Engineering / Data%20and%20Analytics ...
    (optional) &location=New%20York%2C%20NY ...

This is a SENTINEL / AGGREGATOR source (like Himalayas / Remotive / HN hiring):
it lives as a SINGLE company row in the DB (name="The Muse", career_url="themuse",
ats_type="themuse"). fetch_jobs IGNORES company.career_url entirely and pages the
public API across the tech/data categories. Each payload result carries its OWN
employer, so normalize_job sets company_name from the PARSED employer
(result.company.name) — NOT company.name — and the dedupe hash is built on
employer + title + location + url so two different employers can't collide.

API SHAPE (curl-verified 2026-06-19):
    top level: {page, page_count, items_per_page, took, timed_out, total,
                results:[...], aggregations}
    result:    name (=title), type, publication_date:<ISO 8601 "...Z">,
               short_name, model_type, id,
               locations:[{name:<"City, ST" | "City, Country" |
                           "Flexible / Remote">}],
               categories:[{name}], levels:[{name, short_name}], tags,
               refs:{landing_page:<canonical themuse url>},
               company:{id, short_name, name},
               contents:<HTML description>

CATEGORIES (curl-verified — the API SILENTLY IGNORES unknown category names and
returns ~everything, so we only use names that actually filter):
    "Software Engineering"  (~101k jobs)
    "Data and Analytics"    (~17k jobs)
    "Computer and IT"       (~1.6k jobs)
    "Science and Engineering" (~8k jobs)
NOTE: "Data Science" is NOT a valid Muse category (returns ~0 real hits); the
data bucket is "Data and Analytics".

PAGINATION: `page` is 1-based; page_count is huge (tens of thousands) because it
counts ALL jobs, so we cap at MAX_PAGES per category. items_per_page is 20. The
feed is sorted newest-first by publication_date, so paging walks back in time —
exactly what a 24/7 crawler with short posted-date retention wants. Results are
deduped by id across categories before normalization.

US FILTER: locations[].name is a free-text "City, ST" (US, 2-letter state code),
"City, Country" (international), or "Flexible / Remote". We keep a job if ANY of
its locations is US-relevant: a US state/territory 2-letter code, an explicit
"United States"/"USA" token, or a remote/anywhere/worldwide marker. The 2-letter
state code disambiguates "Decatur, GA" (US-Georgia, kept) from "Zestap'oni,
Georgia" (country, dropped). location stored on the Job is the US-relevant
location(s) only.

description (contents) is HTML -> clean_html + truncate. posted_at comes from
publication_date (ISO 8601; parse_date handles it). No detail call is needed —
the list payload already has description + date — so there is NO
enrich_posted_date.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://www.themuse.com/api/public/jobs?page={page}&category={category}"
MAX_PAGES = 20  # per category; 20 pages * 20/page = up to 400 newest per category

# Tech/data Muse categories that ACTUALLY filter server-side (verified). One run
# pulls software + data + IT + science/eng jobs.
CATEGORIES = (
    "Software Engineering",
    "Data and Analytics",
    "Computer and IT",
    "Science and Engineering",
)

# 50 states + DC + inhabited territories — used as 2-letter location suffix codes.
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
}

# US-relevant / US-friendly markers, matched as WHOLE WORDS (word boundaries).
# The Muse uses "Flexible / Remote" as its ONE remote marker; "remote" covers it.
# Speculative worldwide-bucket tokens ("global"/"worldwide"/"anywhere") are
# DELIBERATELY EXCLUDED: a 20-page scan (2026-06-19) found they never appear as a
# real Muse remote marker — the only "global" hit was the foreign city
# "Bonifacio Global City, Philippines", which we must NOT keep.
_US_TOKENS = (
    "united states",
    "usa",
    "u.s.",
    "remote",
)
_US_TOKEN_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(t) for t in _US_TOKENS) + r")(?![a-z])"
)


def _is_us_location(loc_name: str) -> bool:
    """True if a single location string is US-relevant / US-friendly.

    Matches a trailing 2-letter US state code ("City, ST"), an explicit US token,
    or a remote marker (as a whole word). The state-code check is what
    distinguishes the US state "GA" from the country "Georgia".
    """
    name = (loc_name or "").strip()
    if not name:
        return False
    if _US_TOKEN_RE.search(name.lower()):
        return True
    # "City, ST" -> last comma-separated chunk is a 2-letter US state code.
    tail = name.rsplit(",", 1)[-1].strip().upper()
    return tail in _US_STATE_CODES


def _us_locations(locations: List[Dict[str, Any]]) -> List[str]:
    """Return the US-relevant location name(s) from a result's locations list."""
    out: List[str] = []
    for loc in locations or []:
        nm = (loc.get("name") or "").strip()
        if nm and _is_us_location(nm):
            out.append(nm)
    return out


class TheMuseCrawler(BaseCrawler):
    source_name = "themuse"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "themuse" or "themuse.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Page the public API per tech/data category, newest-first.

        Ignores company.career_url. Dedupes results by id across categories and
        keeps only US-relevant postings (>=1 US/remote location). Stops a
        category early on an empty/short page so we don't page past the feed.
        """
        seen_ids: set = set()
        out: List[Dict[str, Any]] = []
        for category in CATEGORIES:
            for page in range(1, MAX_PAGES + 1):
                url = API.format(page=page, category=category.replace(" ", "%20"))
                data = self._get(url).json()
                results = data.get("results", []) or []
                if not results:
                    break
                for raw in results:
                    job_id = raw.get("id")
                    if job_id is not None and job_id in seen_ids:
                        continue
                    us_locs = _us_locations(raw.get("locations"))
                    if not us_locs:
                        continue
                    if job_id is not None:
                        seen_ids.add(job_id)
                    # stash the filtered US locations so normalize_job can reuse
                    # them without re-running the filter.
                    raw["_us_locations"] = us_locs
                    out.append(raw)
                # page_count counts ALL jobs (huge); rely on MAX_PAGES + a short
                # page as the stop condition instead.
                if len(results) < (data.get("items_per_page") or 20):
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = ((raw.get("company") or {}).get("name") or "").strip()
        title = (raw.get("name") or "").strip()
        # Prefer the US-relevant locations captured in fetch_jobs; fall back to
        # re-filtering if this is called directly.
        us_locs = raw.get("_us_locations") or _us_locations(raw.get("locations"))
        location = ", ".join(us_locs)
        job_url = ((raw.get("refs") or {}).get("landing_page") or "").strip()
        description = truncate(clean_html(raw.get("contents") or ""))
        posted_at = parse_date(raw.get("publication_date"))
        employment_type = (raw.get("type") or "").strip()

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
