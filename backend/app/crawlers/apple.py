"""
crawlers/apple.py
-----------------
Apple's official careers site (jobs.apple.com). No public JSON API, but
the search page is server-rendered React with a `window.__staticRouterHydrationData`
JSON blob that carries every visible job as `loaderData.search.searchResults[]`.

Fields available per job:
  positionId       (Apple's own id, used to build the canonical apply URL)
  postingTitle
  jobSummary       (~1-line abstract; the full JD requires a detail fetch
                    which we skip for throughput — Apple's search summary is
                    typically 300-800 chars, enough for filter_engine gates)
  locations[]      each with postLocationId ('postLocation-USA', etc), city,
                    stateProvince, countryName
  postingDate      ISO-ish 'Jul 30, 2026' string; we parse or leave None

Registered as a single Company (ats_type='apple', career_url='apple') the
same way Amazon is. fetch_jobs() ignores company.career_url and runs several
role-scoped searches, deduping by positionId.

POSTED DATE
-----------
Apple exposes a real 'postingDate' so no crawl-time fallback. Unparseable
dates → None so the retention gate can honor NULL rather than pretend today.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

SITE = "https://jobs.apple.com"
SEARCH = f"{SITE}/en-us/search"

# Apple's US location filter id. Determined by inspecting their filter
# facets in the hydration data (loaderData.search.filters.locations[0].id).
US_LOCATION_ID = "postLocation-USA"

# Search terms scoped to Ram's target lanes. Apple has ~4-5k open roles at
# any time; querying a handful of terms and deduping catches the relevant
# 90-95% without pulling the entire posting corpus.
QUERIES = (
    "data engineer",
    "software engineer",
    "machine learning",
    "cloud",
    "backend",
    "site reliability",
    "platform engineer",
)

# Apple returns up to ~20 per page in the hydration payload. Multiple pages
# per query is cheap since the whole page ships in one HTML round-trip.
MAX_PAGES_PER_QUERY = 3

_HYDRATION_RE = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((?P<literal>.+?)\);',
    re.DOTALL,
)


def _extract_hydration(html: str) -> dict:
    """Pull the JSON blob out of window.__staticRouterHydrationData."""
    m = _HYDRATION_RE.search(html)
    if not m:
        return {}
    literal = m.group("literal").strip()
    # It's a JS string literal — decode escapes to get the JSON text
    if (literal.startswith("'") and literal.endswith("'")) or (
        literal.startswith('"') and literal.endswith('"')):
        inner = literal[1:-1].encode("utf-8").decode("unicode_escape")
    else:
        inner = literal
    try:
        return json.loads(inner)
    except (ValueError, TypeError):
        return {}


def _parse_posted(v: str | None) -> datetime | None:
    """'Jul 30, 2026' → datetime; None on any parse failure."""
    if not v:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v.strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def _location_str(locs: List[dict]) -> str:
    """Join Apple's location objects into a readable 'City, State, Country'.
    postLocationId like 'postLocation-USA' means unspecified US location."""
    if not locs:
        return ""
    parts = []
    for loc in locs:
        bits = [loc.get("city"), loc.get("stateProvince"), loc.get("countryName")]
        joined = ", ".join(b for b in bits if b)
        if joined:
            parts.append(joined)
        elif "USA" in (loc.get("postLocationId") or ""):
            parts.append("United States")
    # Dedup while preserving order
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return "; ".join(out)


class AppleJobsCrawler(BaseCrawler):
    source_name = "apple"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("apple", "applejobs") or "jobs.apple.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for q in QUERIES:
            for page in range(1, MAX_PAGES_PER_QUERY + 1):
                params = {
                    "location": "united-states-USA",
                    "search": q,
                    "page": str(page),
                }
                try:
                    resp = self._get(SEARCH, params=params)
                    html = resp.text
                except Exception:  # noqa: BLE001
                    break
                hydration = _extract_hydration(html)
                results = (hydration.get("loaderData", {})
                                    .get("search", {})
                                    .get("searchResults") or [])
                if not results:
                    break
                added_this_page = 0
                for j in results:
                    pid = str(j.get("positionId") or j.get("id") or "")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    out.append(j)
                    added_this_page += 1
                # Short page → last page, move on
                if added_this_page < len(results) // 2 or len(results) < 15:
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("postingTitle") or "").strip()
        summary = (raw.get("jobSummary") or "").strip()
        pid = str(raw.get("positionId") or raw.get("id") or "").strip()
        # Apple's canonical apply URL: /en-us/details/<positionId>/<slug>
        # The slug is optional for open; the id alone resolves.
        job_url = f"{SITE}/en-us/details/{quote(pid)}" if pid else ""
        location = _location_str(raw.get("locations") or [])
        posted = _parse_posted(raw.get("postingDate"))

        # jobSummary is HTML-lite; strip anything weird
        description = truncate(clean_html(summary))

        return Job(
            company_id=company.id,
            title=title,
            company_name="Apple",
            location=location,
            employment_type="Full-time",  # Apple corporate roles are FT
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted,
            raw_data_hash=make_hash("Apple", title, location, job_url),
        )
