"""
crawlers/jsonld.py
------------------
Generic crawler for custom career sites that embed schema.org JobPosting data
in <script type="application/ld+json"> blocks. Standardized by Google for
"Google for Jobs" — every SEO-conscious careers page publishes it. Works
for many custom portals we'd otherwise need per-company scrapers for.

Two patterns:
  1. INDEX page  → the career listing page has multiple JobPosting objects
     (or ItemList of JobPostings). We fetch, extract each, done.
  2. DETAIL page → a listing page links to per-job detail pages that each
     embed one JobPosting. We follow up to N detail links.

Accepts in company.career_url:
    A full URL like https://careers.example.com/openings/
    or a bare domain — we'll try /careers, /jobs, /careers/all as fallbacks.

POSTED DATE
-----------
`datePosted` is standard schema.org and always ISO-8601. Parsed via
utils.dates.parse_flexible_date; None on any failure so the retention gate
honors NULL instead of stamping crawl-time.

Registered as ats_type='jsonld'. scripts/probe_jsonld.py sweeps dormant
companies to fingerprint which ones have this pattern and set the ats_type.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

# Common career page paths to try when the URL is just a bare domain
CAREER_PATHS = (
    "", "/careers", "/careers/", "/careers/open-positions",
    "/careers/all", "/careers/roles", "/careers/jobs",
    "/jobs", "/jobs/", "/join-us", "/join",
    "/opportunities", "/company/careers", "/company/jobs",
    "/about/careers", "/work-with-us",
)

# How many linked detail pages to follow when the listing page shows links but
# not embedded JSON-LD. Capped to keep one company crawl fast.
MAX_DETAIL_FOLLOW = 30
FETCH_TIMEOUT = 20

_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_JOB_LINK_RE = re.compile(
    r'href=["\']([^"\']*(?:job|position|opening|career|role)[^"\']*)["\']',
    re.IGNORECASE,
)


def _extract_json_blocks(html: str) -> List[dict]:
    """Return every JSON-LD object embedded in the HTML. Handles arrays,
    @graph collections, and plain single objects."""
    out: List[dict] = []
    for match in _SCRIPT_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            out.extend(x for x in parsed if isinstance(x, dict))
        elif isinstance(parsed, dict):
            if "@graph" in parsed and isinstance(parsed["@graph"], list):
                out.extend(x for x in parsed["@graph"] if isinstance(x, dict))
            else:
                out.append(parsed)
    return out


def _is_job_posting(obj: dict) -> bool:
    """Match schema.org JobPosting nodes tolerating @type as str or list."""
    t = obj.get("@type") or obj.get("type") or ""
    if isinstance(t, list):
        return any(str(x).lower() == "jobposting" for x in t)
    return str(t).lower() == "jobposting"


def _flatten_location(loc) -> str:
    if not loc:
        return ""
    if isinstance(loc, list):
        return "; ".join(_flatten_location(x) for x in loc if x).strip("; ")
    if not isinstance(loc, dict):
        return str(loc).strip()
    # jobLocation -> address -> ...
    addr = loc.get("address") or loc
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    if not isinstance(addr, dict):
        return str(addr).strip()
    bits = [
        addr.get("addressLocality"),
        addr.get("addressRegion"),
        addr.get("addressCountry", {}).get("name")
            if isinstance(addr.get("addressCountry"), dict)
            else addr.get("addressCountry"),
    ]
    return ", ".join(b for b in bits if b)


def _job_url_from(obj: dict, page_url: str) -> str:
    """Try obj.url → hiringOrganization.sameAs → identifier.url → page_url."""
    for key in ("url", "hiringOrganization"):
        v = obj.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            u = v.get("url") or v.get("sameAs")
            if isinstance(u, str) and u.startswith("http"):
                return u
    ident = obj.get("identifier")
    if isinstance(ident, dict):
        v = ident.get("value") or ident.get("url")
        if isinstance(v, str) and v.startswith("http"):
            return v
    return page_url


class JSONLDCrawler(BaseCrawler):
    source_name = "jsonld"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s in ("jsonld", "json-ld", "schema-org") or s.startswith("jsonld:")

    def _fetch(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code != 200 or not r.text:
                return None
            return r.text
        except Exception:
            return None

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        base_url = (company.career_url or "").strip()
        if not base_url:
            return []
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url

        # 1) Load the primary career URL, extract any embedded JobPostings.
        html = self._fetch(base_url)
        if not html:
            return []

        found: List[Dict[str, Any]] = []
        seen_urls: set[str] = set()
        for obj in _extract_json_blocks(html):
            if _is_job_posting(obj):
                obj["_source_url"] = _job_url_from(obj, base_url)
                found.append(obj)
                seen_urls.add(obj["_source_url"])

        # 2) If the listing page has < 2 embedded JobPostings, treat it as a
        #    hub with links to detail pages and follow up to MAX_DETAIL_FOLLOW.
        if len(found) < 2:
            candidate_links: List[str] = []
            for m in _JOB_LINK_RE.finditer(html):
                link = m.group(1).strip()
                if not link or link.startswith("#") or link.startswith("javascript:"):
                    continue
                abs_url = urljoin(base_url, link)
                # Keep links on the same host — avoids following out to LinkedIn/Indeed
                if urlparse(abs_url).netloc != urlparse(base_url).netloc:
                    continue
                if abs_url in seen_urls:
                    continue
                candidate_links.append(abs_url)
                if len(candidate_links) >= MAX_DETAIL_FOLLOW:
                    break

            for link in candidate_links:
                detail_html = self._fetch(link)
                if not detail_html:
                    continue
                for obj in _extract_json_blocks(detail_html):
                    if _is_job_posting(obj):
                        obj["_source_url"] = _job_url_from(obj, link)
                        if obj["_source_url"] in seen_urls:
                            continue
                        seen_urls.add(obj["_source_url"])
                        found.append(obj)
        return found

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        location = _flatten_location(raw.get("jobLocation")).strip()
        posted = parse_date(raw.get("datePosted"))
        etype_raw = raw.get("employmentType") or ""
        if isinstance(etype_raw, list):
            etype_raw = " ".join(str(x) for x in etype_raw)
        etype = str(etype_raw).replace("_", " ").title().strip()
        job_url = raw.get("_source_url") or ""
        hiring_org = raw.get("hiringOrganization") or {}
        if isinstance(hiring_org, dict):
            employer = (hiring_org.get("name") or company.name or "").strip()
        else:
            employer = company.name

        return Job(
            company_id=company.id,
            title=title,
            company_name=employer,
            location=location,
            employment_type=etype or "Full-time",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted,
            raw_data_hash=make_hash(employer, title, location, job_url),
        )
