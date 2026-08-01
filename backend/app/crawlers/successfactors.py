"""
crawlers/successfactors.py
--------------------------
SAP SuccessFactors Recruiting Marketing (RMK) — per-company ATS with a huge
Fortune-500 footprint. Two eras coexist:

    Legacy: https://career4.successfactors.com/career?company={companyId}&career_ns=job_listing_summary&resultType=XML
    Newer:  https://{tenant}.jobs.hr.cloud.sap/sitemap-jobs.xml  (Career Site Builder)

We accept either shape in company.career_url:

    "sap"                                 -> legacy: companyId="sap"
    "jpmc|jpmc"                           -> pipe form: (companyId, tenant)
    "https://career4.successfactors.com/career?company=sap"
    "https://sap.jobs/sitemap-jobs.xml"
    "https://careers.sap.com"             -> tries the sitemap under that host

XML shape (legacy): <jobs><job><jobReqId/><jobTitle/><location/><postedDate/>
<jobDescription/><externalUrl/></job>...</jobs>

Sitemap shape (newer): standard <urlset> with <loc>+<lastmod>. lastmod is
job-page freshness, not necessarily posted date, but it's a defensible fallback
until we detail-fetch each URL.

No auth. No key. Some tenants front Cloudflare — we already send a real
User-Agent (settings.user_agent) so this rarely blocks.

Follow-up detail fetches are cheap and can be added later if descriptions
matter for filtering; the sitemap-only path returns URL + freshness only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

LEGACY_URL = "https://career4.successfactors.com/career?company={company_id}&career_ns=job_listing_summary&resultType=XML"


def _extract_tokens(career_url: str) -> Tuple[str, str]:
    """Return (legacy_company_id, csb_host). Either may be empty.

    csb_host is the newer *.jobs.hr.cloud.sap host if we can find one; otherwise
    any host that looks like a careers site we can probe for /sitemap-jobs.xml.
    """
    s = (career_url or "").strip()
    if not s:
        return "", ""

    if "|" in s:
        legacy, host = s.split("|", 1)
        return legacy.strip(), host.strip()

    m = re.search(r"[?&]company=([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1), ""

    # Bare token — treat as legacy companyId.
    if "/" not in s and "." not in s:
        return s, ""

    # Otherwise assume it's a URL for the CSB path (any careers host).
    if not s.startswith("http"):
        s = "https://" + s
    host = urlparse(s).hostname or ""
    return "", host


def _xpath_text(elem, tag: str) -> str:
    node = elem.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


class SuccessFactorsCrawler(BaseCrawler):
    source_name = "successfactors"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return (
            s in ("successfactors", "sap_successfactors", "sf")
            or "successfactors.com" in s
            or "jobs.hr.cloud.sap" in s
        )

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        legacy_id, csb_host = _extract_tokens(company.career_url)

        # Path 1: legacy XML job listing.
        if legacy_id:
            try:
                r = self._get(LEGACY_URL.format(company_id=legacy_id))
                root = ET.fromstring(r.content)
                jobs = []
                for j in root.iter("job"):
                    jobs.append(
                        {
                            "_mode": "legacy",
                            "id": _xpath_text(j, "jobReqId"),
                            "title": _xpath_text(j, "jobTitle"),
                            "location": _xpath_text(j, "location"),
                            "posted": _xpath_text(j, "postedDate"),
                            "description": _xpath_text(j, "jobDescription"),
                            "url": _xpath_text(j, "externalUrl") or _xpath_text(j, "jobDetailUrl"),
                        }
                    )
                if jobs:
                    return jobs
            except Exception:  # noqa: BLE001
                pass

        # Path 2: CSB sitemap.
        if csb_host:
            for path in ("/sitemap-jobs.xml", "/sitemap.xml"):
                try:
                    r = self._get(f"https://{csb_host}{path}")
                    root = ET.fromstring(r.content)
                    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                    urls = root.findall("sm:url", ns) or root.findall(".//url")
                    if not urls:
                        continue
                    jobs = []
                    for u in urls:
                        loc = u.find("sm:loc", ns) if u.find("sm:loc", ns) is not None else u.find("loc")
                        lastmod = u.find("sm:lastmod", ns) if u.find("sm:lastmod", ns) is not None else u.find("lastmod")
                        href = (loc.text or "").strip() if loc is not None else ""
                        if not href or "/job/" not in href.lower() and "/careers/" not in href.lower():
                            continue
                        jobs.append(
                            {
                                "_mode": "sitemap",
                                "url": href,
                                "posted": (lastmod.text or "").strip() if lastmod is not None else "",
                                # Title comes from the URL slug — a decent fallback.
                                "title": _slug_title(href),
                                "location": "",
                                "description": "",
                            }
                        )
                    if jobs:
                        return jobs
                except Exception:  # noqa: BLE001
                    continue

        return []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or "").strip()
        job_url = (raw.get("url") or "").strip()
        description = truncate(clean_html(raw.get("description") or ""))
        posted_at = parse_date(raw.get("posted"))

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )


def _slug_title(url: str) -> str:
    """Best-effort title from a job URL slug ('/job/senior-data-engineer-remote-12345')."""
    m = re.search(r"/(?:job|jobs|careers)/([^/?#]+)", url, re.I)
    if not m:
        return ""
    slug = m.group(1)
    slug = re.sub(r"[-_]?\d+$", "", slug)  # strip trailing job id
    return slug.replace("-", " ").replace("_", " ").strip().title()
