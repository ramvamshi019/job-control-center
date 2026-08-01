"""
crawlers/taleo.py
-----------------
Oracle Taleo — legacy but still widely used in finance/pharma/retail.
No auth, no key.

Endpoint (POST JSON):
    https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs
      ?lang=en&portal={portalId}

company.career_url accepts:
    "{tenant}|{portalId}"                              (pipe form)
    "https://{tenant}.taleo.net/careersection/1/..."   (we parse tenant from host, portal from path)
    bare "{tenant}"                                    (portal defaults to "1")

Response: {"requestStatus":{...}, "totalCount":N, "pageSize":M,
    "requisitionList":[{"contest":..., "jobId":..., "column":[{"colName":"Title","colValue":"..."}, ...]}, ...]}

The `column` array carries fields keyed by colName (Title, Location, Posted,
JobField, JobShift, ...). We pull what we recognize. Descriptions are
NOT in the list response — they live at
    /careersection/{section}/jobdetail.ftl?job={contest}
so we skip descriptions here; filter engines can still act on title+location.

Some tenants front Akamai bot mgmt; the default UA + delay is usually enough.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import make_hash

PAGE_SIZE = 100
MAX_PAGES = 10


def _extract_tenant_portal(career_url: str) -> Tuple[str, str]:
    s = (career_url or "").strip()
    if not s:
        return "", ""
    if "|" in s:
        t, p = s.split("|", 1)
        return t.strip(), p.strip() or "1"
    if "taleo.net" in s.lower():
        url = s if s.startswith("http") else "https://" + s
        host = urlparse(url).hostname or ""
        tenant = host.split(".")[0] if host else ""
        m = re.search(r"/careersection/(\d+)", url)
        portal = m.group(1) if m else "1"
        return tenant, portal
    # Bare tenant name.
    if "/" not in s and "." not in s:
        return s, "1"
    return "", ""


def _search_url(tenant: str, portal: str) -> str:
    return f"https://{tenant}.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}"


def _job_url(tenant: str, portal: str, contest: str) -> str:
    return f"https://{tenant}.taleo.net/careersection/{portal}/jobdetail.ftl?job={contest}"


def _columns(item: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for c in item.get("column") or []:
        if isinstance(c, dict):
            name = str(c.get("colName") or "").strip()
            val = str(c.get("colValue") or "").strip()
            if name:
                out[name] = val
    return out


class TaleoCrawler(BaseCrawler):
    source_name = "taleo"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "taleo" or "taleo.net" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        tenant, portal = _extract_tenant_portal(company.career_url)
        if not tenant:
            return []
        url = _search_url(tenant, portal)

        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            body = {
                "multilineEnabled": False,
                "sortingSelection": {"sortBy": "3", "ascendingSortingOrder": False},
                "fieldData": {"fields": {"KEYWORD": ""}, "valid": True},
                "filterSelectionParams": {"searchFilterSelections": []},
                "pageNo": page,
            }
            try:
                # base._get is a GET; use session.post directly.
                r = self.session.post(url, json=body, timeout=30)
                r.raise_for_status()
                data = r.json()
            except Exception:  # noqa: BLE001
                break

            batch = data.get("requisitionList") or []
            if not batch:
                break

            for item in batch:
                if not isinstance(item, dict):
                    continue
                contest = str(item.get("contest") or item.get("jobId") or "")
                if not contest or contest in seen:
                    continue
                seen.add(contest)
                out.append({"_tenant": tenant, "_portal": portal, "_contest": contest, **item})

            total = int(data.get("totalCount") or 0)
            if page * PAGE_SIZE >= total:
                break

        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        cols = _columns(raw)
        title = cols.get("Title", "").strip()
        location = cols.get("Location", "").strip()
        posted_at = parse_date(cols.get("PostingDate") or cols.get("Posted"))
        job_url = _job_url(raw["_tenant"], raw["_portal"], raw["_contest"])

        emp_type = cols.get("JobType", "") or cols.get("JobShift", "")

        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=emp_type.strip(),
            job_url=job_url,
            source=self.source_name,
            description="",
            posted_at=posted_at,
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
