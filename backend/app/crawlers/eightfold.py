"""
crawlers/eightfold.py
---------------------
Eightfold AI ATS (SmartApply) public job boards — the enterprise / Fortune-500
boards our token crawlers (Greenhouse/Lever/Ashby/…) never see. No auth needed.

QUIRK: an Eightfold board is identified by BOTH a subdomain slug AND a `domain=`
query param, and they are NOT always the same string. The list endpoint is:

    https://{slug}.eightfold.ai/api/apply/v2/jobs?domain={domain}&hl=en&start=0

so we need both pieces. We store them PIPE-DELIMITED in company.career_url as
"slug|domain" (e.g. "netflix|netflix.com") and split them in extract_token — the
same pipe convention the repo already uses for Workday tenants. A full board URL
("https://netflix.eightfold.ai/careers") is also accepted: the slug is parsed
from the host and the domain is inferred from the slug.

PAGINATION (curl-verified 2026-06-19): the API returns 10 positions per page in
`positions[]` and the grand total in `count` (older boards may call it
`totalJobs` — we accept either). We walk `start` in steps of 10 until we have
fetched `count` jobs or hit MAX_PAGES.

LIST PAYLOAD (per position): id (numeric job id for the detail call),
name (title), location (str), locations ([str]), department, type,
t_create / t_update (epoch SECONDS), canonicalPositionUrl (the real apply URL on
the employer's own careers host), job_description (EMPTY in the list — only the
detail call populates it).

DETAIL (per NEW job, in enrich_posted_date — cheap, post-dedupe only):
    https://{slug}.eightfold.ai/api/apply/v2/jobs/{id}?domain={domain}&hl=en
returns the same object WITH a full HTML `job_description`.

US FILTER: these are global boards (Netflix, Bayer, …), so we keep only
US / US-remote / worldwide-remote postings (see _is_us_relevant). Non-US-locked
listings (e.g. "Berlin,Germany") are dropped.

This is a PER-COMPANY source (one DB row == one employer's board), so
company_name is company.name and the dedupe hash is company.name + job_url —
unlike the himalayas/hn_hiring aggregators which parse the employer per job.

VERIFIED LIVE 2026-06-19 (no auth, HTTP 200 with real jobs):
    netflix | netflix.com   (count 518, US-heavy: data/ML/eng roles)
    bayer   | bayer.com     (count 744, global; US roles kept by the filter)
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LIST = "https://{slug}.eightfold.ai/api/apply/v2/jobs?domain={domain}&hl=en&start={start}"
DETAIL = "https://{slug}.eightfold.ai/api/apply/v2/jobs/{jid}?domain={domain}&hl=en"

PAGE = 10        # the API returns exactly 10 positions/page
MAX_PAGES = 60   # safety cap: 60 * 10 = up to 600 newest jobs per board/run

# Substrings (lowercased) that mark a location as US-relevant / US-friendly.
# Worldwide / anywhere remote roles are US-friendly by definition.
US_TOKENS = (
    "united states",
    "usa",
    "u.s.a",
    "u.s.",
    ", us",
    "us -",
    "us-",
    "-us",
    "north america",
    "americas",
    "anywhere",
    "worldwide",
    "global",
    "remote - us",
)


def extract_token(career_url: str) -> Optional[Tuple[str, str]]:
    """Return (slug, domain) from a 'slug|domain' string or an eightfold URL.

    - "netflix|netflix.com"                         -> ("netflix", "netflix.com")
    - "netflix"                                      -> ("netflix", "netflix.com")  (domain inferred)
    - "https://netflix.eightfold.ai/careers"        -> ("netflix", "netflix.com")
    Returns None if no slug can be determined.
    """
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return None

    # Preferred form: pipe-delimited slug|domain (repo tolerates pipes already).
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        if len(parts) >= 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            slug = parts[0]
            return slug, f"{slug}.com"
        return None

    # Full eightfold board URL -> pull the slug from the host.
    m = re.search(r"https?://([A-Za-z0-9_-]+)\.eightfold\.ai", s)
    if m:
        slug = m.group(1)
        return slug, f"{slug}.com"

    # Bare slug (no scheme, no dot) -> infer "slug.com" as the domain.
    if "/" not in s and "." not in s:
        return s, f"{s}.com"

    # Anything else (e.g. a bare host) -> first label is the slug.
    host = re.sub(r"^https?://", "", s).split("/")[0]
    slug = host.split(".")[0]
    return (slug, f"{slug}.com") if slug else None


def _locations(raw: Dict[str, Any]) -> List[str]:
    locs = raw.get("locations")
    if isinstance(locs, list) and locs:
        return [str(x) for x in locs if x]
    one = raw.get("location")
    return [str(one)] if one else []


def _is_us_relevant(raw: Dict[str, Any]) -> bool:
    """True if any of the posting's locations is US / US-remote / worldwide."""
    locs = [l.lower() for l in _locations(raw)]
    if not locs:
        # No location at all -> keep (often a "remote / multiple" listing).
        return True
    return any(any(tok in loc for tok in US_TOKENS) for loc in locs)


class EightfoldCrawler(BaseCrawler):
    source_name = "eightfold"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "eightfold" or "eightfold.ai" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        parsed = extract_token(company.career_url)
        if not parsed:
            return []
        slug, domain = parsed

        out: List[Dict[str, Any]] = []
        total: Optional[int] = None
        for page in range(MAX_PAGES):
            start = page * PAGE
            url = LIST.format(slug=slug, domain=domain, start=start)
            data = self._get(url, headers={"Accept": "application/json"}).json()

            positions = data.get("positions") or []
            if not positions:
                break

            if total is None:
                # newer boards: "count"; older boards: "totalJobs"
                total = data.get("count")
                if total is None:
                    total = data.get("totalJobs")

            for raw in positions:
                if _is_us_relevant(raw):
                    # carry the board coords so enrich can build the detail URL
                    raw["_slug"] = slug
                    raw["_domain"] = domain
                    out.append(raw)

            fetched = start + len(positions)
            if len(positions) < PAGE:
                break  # short page -> end of feed
            if total is not None and fetched >= total:
                break
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("name") or raw.get("posting_name") or "").strip()
        location = ", ".join(_locations(raw))
        # Use the eightfold-hosted job URL as the canonical link: it carries the
        # slug + numeric id + domain (so enrich_posted_date can always rebuild
        # the detail call from it) and 307-redirects to the employer's own apply
        # page anyway. Fall back to canonicalPositionUrl only if we can't build
        # the eightfold form (no id), so we never emit an empty link.
        jid = raw.get("id")
        slug = raw.get("_slug", "")
        domain = raw.get("_domain", "")
        if jid and slug:
            job_url = (
                f"https://{slug}.eightfold.ai/careers/job/{jid}?domain={domain}"
            )
        else:
            job_url = (raw.get("canonicalPositionUrl") or "").strip()
        # t_create / t_update are epoch SECONDS; prefer the original post date.
        posted_at = parse_date(raw.get("t_create")) or parse_date(raw.get("t_update"))
        employment_type = (raw.get("type") or "").strip()
        if employment_type.upper() == "ATS":
            employment_type = ""  # "ATS" is a source tag, not a job type

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,            # per-company board, not an aggregator
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description="",                       # filled by enrich_posted_date
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> Job:
        """The list payload has the date but NOT the description — fetch the JD
        from the per-job detail endpoint. Called by the pipeline for NEW jobs
        only (post-dedupe), so it stays cheap. On any failure we leave the job
        unchanged (it keeps its list-derived posted_at) so a transient detail
        error never drops a posting.

        The detail URL needs the numeric job id + the slug|domain coords, which
        we recover from the job_url (canonical host carries the id; the
        eightfold detail call needs the eightfold slug + domain param)."""
        slug, domain, jid = self._detail_coords(job)
        if not (slug and domain and jid):
            return job
        try:
            d = self._get(
                DETAIL.format(slug=slug, domain=domain, jid=jid),
                headers={"Accept": "application/json"},
            ).json()
        except Exception:  # noqa: BLE001 - keep the list values on failure
            return job

        desc = d.get("job_description")
        if isinstance(desc, str) and desc.strip():
            job.description = truncate(clean_html(desc))
        # refresh the date if the detail call has a better one
        posted = parse_date(d.get("t_create")) or parse_date(d.get("t_update"))
        if posted:
            job.posted_at = posted
        return job

    @staticmethod
    def _detail_coords(job: Job) -> Tuple[str, str, str]:
        """Recover (slug, domain, job_id) for the detail call from job_url.

        normalize_job sets job_url to the eightfold-hosted form
            https://{slug}.eightfold.ai/careers/job/{jid}?domain={domain}
        which carries all three pieces, so a single parse is enough. If job_url
        is a non-eightfold canonical link (the rare no-id fallback) we can still
        grab the id but not the slug/domain, and return blanks for those so the
        caller leaves the job unchanged."""
        url = job.job_url or ""
        m_id = re.search(r"/careers/job/(\d+)", url)
        jid = m_id.group(1) if m_id else ""
        m = re.search(r"https?://([A-Za-z0-9_-]+)\.eightfold\.ai", url)
        slug = m.group(1) if m else ""
        m_dom = re.search(r"[?&]domain=([^&]+)", url)
        domain = m_dom.group(1) if m_dom else (f"{slug}.com" if slug else "")
        return slug, domain, jid
