"""
crawlers/workday.py
-------------------
Workday public job board API (no key needed). Workday has no single token: each
company is identified by tenant + data-center pod + site path, e.g.
    nvidia.wd5.myworkdayjobs.com/.../NVIDIAExternalCareerSite

We store that in company.career_url as a pipe string "tenant|dc|site"
(e.g. "nvidia|wd5|NVIDIAExternalCareerSite"), or a full myworkdayjobs.com URL.

API (POST, paginated 20 at a time):
    https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
The list gives postedOn as a relative string ("Posted 7 Days Ago"), which we
convert to a real date so the 30-day retention/pruning still works.

The list endpoint deliberately returns only card-level fields (title, location,
postedOn, path) — no real description and a collapsed "N Locations" placeholder
when a role is multi-site. That was breaking the US-location gate (20k+
rejections/day of legit US roles that just showed as "2 Locations") and
starving the filter/score signal for 44k+ Workday jobs/day. `enrich_posted_date`
below re-hits a per-job detail endpoint to fill in the full jobDescription,
the real primary location + additionalLocations list, and a precise postedOn.
The scheduler only calls enrichment on NEW jobs (dedupe runs first), so the
extra requests are bounded to actually-fresh postings.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.logging import get_logger
from app.utils.text import clean_html, make_hash, truncate

log = get_logger("crawler.workday")

PAGE = 20
MAX_PAGES = 15  # cap ~300 jobs/company to keep big crawls bounded


def parse_career_url(career_url: str) -> Optional[Tuple[str, str, str]]:
    """Return (tenant, dc, site) from 'tenant|dc|site' or a myworkdayjobs URL."""
    s = (career_url or "").strip()
    if "|" in s:
        parts = s.split("|")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return None
    m = re.search(r"https?://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([^/?]+)", s)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _posted_at(posted_on: str) -> datetime:
    """Convert 'Posted 7 Days Ago' / 'Posted Today' to an approximate datetime."""
    now = datetime.utcnow()
    t = (posted_on or "").lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)
    return now  # unknown -> treat as just seen


class WorkdayCrawler(BaseCrawler):
    source_name = "workday"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "workday" or "myworkdayjobs.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        parsed = parse_career_url(company.career_url)
        if not parsed:
            return []
        tenant, dc, site = parsed
        url = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            time.sleep(max(0.0, settings.crawl_delay_seconds))
            resp = self.session.post(
                url, headers=headers,
                json={"limit": PAGE, "offset": offset, "searchText": "", "appliedFacets": {}},
                timeout=settings.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("jobPostings", []) or []
            out.extend(batch)
            total = data.get("total", 0)
            offset += PAGE
            if not batch or offset >= total:
                break
        # stash host info for normalize_job (job URLs)
        for j in out:
            j["_host"] = f"{tenant}.{dc}.myworkdayjobs.com"
            j["_site"] = site
        return out

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        location = (raw.get("locationsText") or "").strip()
        path = raw.get("externalPath") or ""
        host = raw.get("_host", "")
        site = raw.get("_site", "")
        job_url = f"https://{host}/en-US/{site}{path}" if host and path else ""
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type="",
            job_url=job_url,
            source=self.source_name,
            # Placeholder — enrich_posted_date() fills in the real body when the
            # scheduler runs enrichment on new jobs.
            description=" · ".join(p for p in [title, location] if p),
            posted_at=_posted_at(raw.get("postedOn") or ""),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )

    def enrich_posted_date(self, job: Job) -> None:
        """Fill in real description + real US location by hitting the detail
        endpoint. Called by the scheduler on NEW jobs only.

        Named `enrich_posted_date` for backwards compatibility with the
        existing scheduler hook (bamboohr.py uses the same name). In this
        crawler it fills in more than the date:
          - description: full HTML body (cleaned + truncated)
          - location:    primary + additionalLocations joined
          - posted_at:   exact ISO date from the detail endpoint

        Any error is swallowed — a failed enrichment just leaves the card-level
        fields in place, so a bad tenant can't kill an entire crawl.
        """
        detail_url = self._detail_url_for(job.job_url)
        if not detail_url:
            return
        try:
            resp = self.session.get(
                detail_url, timeout=settings.request_timeout_seconds,
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                return
            info = (resp.json() or {}).get("jobPostingInfo") or {}
        except Exception:
            return

        # Real primary location — no more "3 Locations" placeholder.
        primary = (info.get("location") or "").strip()
        extras = info.get("additionalLocations") or []
        if isinstance(extras, list) and extras:
            locs = [primary] + [str(x).strip() for x in extras if x]
        else:
            locs = [primary]
        combined = " / ".join([l for l in locs if l])
        if combined:
            job.location = combined

        # Real body (jobDescription is HTML; strip + cap).
        raw_desc = (info.get("jobDescription") or "").strip()
        if raw_desc:
            job.description = truncate(clean_html(raw_desc))

        # Precise posted date if the detail exposes one.
        real_posted = info.get("startDate") or info.get("postedOn") or ""
        d = _iso_or_relative(real_posted)
        if d:
            job.posted_at = d

    # --- helpers ---
    @staticmethod
    def _detail_url_for(job_url: str) -> Optional[str]:
        """Convert /en-US/{site}/job/... to /wday/cxs/{tenant}/{site}/job/...
        which is the JSON detail endpoint."""
        if not job_url:
            return None
        m = re.match(
            r"https?://(?P<host>[^/]+\.myworkdayjobs\.com)"
            r"/en-US/(?P<site>[^/]+)(?P<rest>/.*)$",
            job_url,
        )
        if not m:
            return None
        host = m.group("host")
        tenant = host.split(".", 1)[0]
        site = m.group("site")
        rest = m.group("rest")
        return f"https://{host}/wday/cxs/{tenant}/{site}{rest}"


def _iso_or_relative(v: str) -> Optional[datetime]:
    """Detail endpoint returns YYYY-MM-DD sometimes, relative strings other
    times. Handle both; return None if neither parses."""
    if not v:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return _posted_at(v)
