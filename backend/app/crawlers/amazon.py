"""
crawlers/amazon.py
------------------
Amazon Jobs (amazon.jobs) — public JSON search API, no key, no auth.

    https://www.amazon.jobs/en/search.json?...

Amazon is one of the largest H-1B sponsors in the US, and their public board
was returning essentially zero coverage before this crawler (existing hits
came from indirect sources: 8 jobs in the last 30d via new_grad only).
Direct API integration lifts that to hundreds/day at steady state.

SENTINEL SOURCE:
    Registered as a single Company row (ats_type='amazon',
    career_url='amazon'). fetch_jobs ignores company.career_url and runs
    several searches ("data engineer", "software engineer", "machine
    learning") each capped at MAX_PAGES pages of PAGE_SIZE results, so a
    single run bounds itself independent of query volume.

WHY MULTIPLE QUERIES INSTEAD OF `q=""`:
    amazon.jobs' unqualified search returns ~50k roles across all categories
    (warehouse, ops, retail). Filtering to tech categories server-side needs
    facet params that break frequently. Cheaper + more robust to run 3-4
    targeted queries covering Ram's actual scope, then let filter_engine and
    the tech-title allowlist do the second-pass gating.

DEDUPE: postings often appear under multiple search queries. We collapse
    duplicates by Amazon's `id` (icims id) inside this crawler before returning
    so the scheduler's per-Job dedupe doesn't have to chew through re-seen work.

POSTED DATE: Amazon exposes a real `posted_date` ("July 28, 2026") — no
    crawl-time fallback needed, so this source has real freshness signal.

LOCATION: `normalized_location` is a clean "City, State, Country" string.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.text import clean_html, make_hash, truncate

SITE = "https://www.amazon.jobs"
SEARCH = f"{SITE}/en/search.json"

# Ram's actual target scope. Broad-but-bounded queries keep the fetch fast
# while covering data/software/ML/cloud/security tech categories. Expanded
# 6->13 queries 2026-07-28 to widen coverage per Ram's ask; dedup on Amazon's
# own posting id in fetch_jobs() means the extra queries add ~200-400 unique
# jobs per crawl without duplicating what the first 6 already found.
QUERIES = (
    "data engineer",
    "software engineer",
    "software development engineer",
    "machine learning",
    "applied scientist",
    "cloud engineer",
    "backend engineer",
    "python developer",
    "java developer",
    "sre",
    "devops engineer",
    "security engineer",
    "systems engineer",
)

PAGE_SIZE = 100
MAX_PAGES_PER_QUERY = 3  # 3 * 100 = up to 300 results per query


def _parse_posted(v: str | None) -> datetime | None:
    """`posted_date` looks like 'July 28, 2026'. Return None on any parse failure
    rather than stamping crawl-time — we want the retention gate to see NULL
    if we can't trust the source, not a fake fresh timestamp."""
    if not v:
        return None
    try:
        return datetime.strptime(v.strip(), "%B %d, %Y")
    except (ValueError, TypeError):
        return None


class AmazonJobsCrawler(BaseCrawler):
    source_name = "amazon"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("amazon", "amazonjobs") or "amazon.jobs" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out: List[Dict[str, Any]] = []
        for q in QUERIES:
            for page in range(MAX_PAGES_PER_QUERY):
                params = {
                    "base_query": q,
                    "country": "USA",
                    "offset": page * PAGE_SIZE,
                    "result_limit": PAGE_SIZE,
                    "sort": "recent",
                }
                try:
                    data = self._get(SEARCH, params=params).json()
                except Exception:  # noqa: BLE001 — one bad query can't break the crawl
                    break
                batch = data.get("jobs") or []
                if not batch:
                    break
                for j in batch:
                    if not isinstance(j, dict):
                        continue
                    jid = str(j.get("id") or j.get("id_icims") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    out.append(j)
                # Stop early on the last non-full page.
                if len(batch) < PAGE_SIZE:
                    break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        # normalized_location is the clean "Fredericksburg, Virginia, USA" string;
        # `location` is a shorter "US, VA, Fredericksburg" that our US-only
        # filter parses less reliably.
        location = (
            raw.get("normalized_location")
            or raw.get("location")
            or ""
        ).strip()

        # Prefer full description over description_short so filter_engine's
        # citizenship / clearance / years-of-experience gates have something
        # substantive to match on (Amazon jobs frequently gate on citizenship).
        desc_html = raw.get("description") or raw.get("description_short") or ""
        # basic_qualifications are the most important gate for scoring / filter.
        bq = raw.get("basic_qualifications") or ""
        pq = raw.get("preferred_qualifications") or ""
        parts = [desc_html]
        if bq:
            parts.append("Basic qualifications: " + bq)
        if pq:
            parts.append("Preferred qualifications: " + pq)
        description = truncate(clean_html(" | ".join(parts)))

        # `job_path` is /en/jobs/<icims-id>/<slug>. `url_next_step` is the direct
        # apply URL — we prefer the canonical job page so the user can read
        # before applying.
        job_url = raw.get("url_next_step") or ""
        path = raw.get("job_path")
        if path:
            job_url = SITE + path

        # Amazon posts multiple legal-entity company_names (Amazon.com Services LLC,
        # Amazon Data Services, Inc., AWS ProServe LLC …). Normalize to "Amazon"
        # so the sponsor lookup and roster hit exactly one row.
        employer = "Amazon"

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type=(raw.get("job_schedule_type") or "").strip(),
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=_parse_posted(raw.get("posted_date")),
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
