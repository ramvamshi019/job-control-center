"""
crawlers/arbeitnow.py
---------------------
Arbeitnow Job Board API (arbeitnow.com) — free public API, NO key / NO auth:

    list: https://www.arbeitnow.com/api/job-board-api
    page: https://www.arbeitnow.com/api/job-board-api?page=2

This is a SENTINEL / AGGREGATOR source (like Himalayas / Remotive / HN hiring):
it lives as a SINGLE company row in the DB (name="Arbeitnow", career_url=
"arbeitnow", ats_type="arbeitnow"). fetch_jobs IGNORES company.career_url
entirely and pages the public API. Each payload job carries its OWN employer, so
normalize_job sets company_name from the PARSED employer (company_name field) —
NOT company.name — and the dedupe hash is built on employer + title + location +
url so two different employers can't collide.

API SHAPE (curl-verified 2026-06-19):
    top level: {data:[...], links:{first,last,prev,next}, meta:{current_page,
               per_page:100, ...}}
    job:       slug, company_name, title, description:<html>, remote:<bool>,
               url:<canonical arbeitnow listing>, tags:[str], job_types:[str],
               location:<free-text e.g. "Munich" / "Berlin, Berlin, Germany">,
               created_at:<epoch seconds>

PAGINATION: 100 jobs/page, ordered by created_at DESCENDING (newest first), so
paging walks back in time — exactly what a 24/7 crawler with short posted-date
retention wants. We page with ?page= and cap at MAX_PAGES. The API is Cloudflare
rate-limited (x-ratelimit-limit: 5/window), so we lean on BaseCrawler._get's
built-in crawl delay and stop early on a short/empty/non-JSON page.

US RELEVANCE (IMPORTANT — verified low): Arbeitnow is an EU/Germany-heavy board.
Across 800 sampled jobs (8 pages, 235 distinct locations) EVERY location was a
German/EU city and there were ZERO US or worldwide-remote postings; even
`remote: true` jobs are remote-WITHIN-Germany (German-language, German-located).
So we apply a STRICT US filter: keep a job ONLY if its location explicitly names
the US (country / state / major city) or is a genuine worldwide/anywhere-remote
bucket. We deliberately do NOT treat the bare `remote` bool as US-friendly,
because on this board it means remote-within-DE. Expect near-zero US volume; the
crawler is wired up correctly so that IF US jobs ever appear they flow through,
but us_relevance is 'low'.

description is HTML -> clean_html + truncate. posted_at comes from created_at
(epoch seconds; parse_date handles it). No detail call is needed — the list
payload already has description + date — so there is NO enrich_posted_date.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://www.arbeitnow.com/api/job-board-api?page={page}"
PAGE_SIZE = 100   # API returns 100 jobs/page (meta.per_page)
MAX_PAGES = 10    # 10 pages * 100 = up to 1000 newest jobs per run

# Substrings (lowercased) that mark a location as explicitly US-relevant. The
# location field is free-text, so we match country/region names, the common
# "City, ST" state abbreviations (padded with separators to avoid matching
# inside other words), and a handful of major US tech hubs. Worldwide/anywhere
# remote buckets are US-friendly by definition.
US_TOKENS = (
    # country / region
    "united states",
    "usa",
    "u.s.",
    "us-remote",
    "remote, us",
    "remote us",
    "remote (us",
    ", us",
    "north america",
    "americas",
    # worldwide / anywhere remote (US-friendly)
    "anywhere",
    "worldwide",
    "remote - worldwide",
    # state abbreviations as ", ST" suffix
    ", ny", ", ca", ", tx", ", wa", ", ma", ", il", ", ga", ", fl",
    ", co", ", or", ", va", ", nc", ", pa", ", nj", ", az", ", dc",
    # major US hubs (spelled out, so we don't rely on abbreviations alone)
    "new york",
    "san francisco",
    "los angeles",
    "california",
    "texas",
    "seattle",
    "boston",
    "chicago",
    "austin",
    "atlanta",
    "denver",
    "washington, d.c.",
)


def _is_us_relevant(location: str) -> bool:
    """True only if the location explicitly names the US / a worldwide-remote
    bucket. Empty or non-US locations are dropped (this board is EU-heavy, so we
    must NOT default-keep blanks the way a worldwide-remote board would)."""
    loc = (location or "").strip().lower()
    if not loc:
        return False
    return any(tok in loc for tok in US_TOKENS)


class ArbeitnowCrawler(BaseCrawler):
    source_name = "arbeitnow"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "arbeitnow" or "arbeitnow.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Page the public API newest-first. Ignores company.career_url.

        Keeps only US-relevant postings (see _is_us_relevant). Stops early on a
        short/empty/non-JSON page so we don't hammer the (rate-limited) API past
        the end of the feed.
        """
        out: List[Dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            resp = self._get(API.format(page=page))
            try:
                data = resp.json()
            except ValueError:
                # Rate-limit / challenge page (non-JSON) -> stop politely.
                break
            jobs = data.get("data", []) or []
            if not jobs:
                break
            for raw in jobs:
                if _is_us_relevant(raw.get("location")):
                    out.append(raw)
            if len(jobs) < PAGE_SIZE:
                break  # reached the end of the feed
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        # Aggregator: the employer comes from the PARSED payload, not company.name.
        employer = (raw.get("company_name") or "").strip()
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        posted_at = parse_date(raw.get("created_at"))
        # job_types is a list (e.g. ["Full Time"], ["Contract"]); join for storage.
        employment_type = ", ".join(
            str(x).strip() for x in (raw.get("job_types") or []) if x
        )

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
