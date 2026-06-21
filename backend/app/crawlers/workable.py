"""
crawlers/workable.py
--------------------
Workable public job widget (no key needed):
    https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
    -> {"name": .., "jobs": [ ... ]}

`token` is the account subdomain. Accepts a bare token or a *.workable.com /
apply.workable.com/{token} URL.

IMPORTANT (root cause of the previous 0-job runs):
    As of mid-2026, the `apply.workable.com` host is fronted by Cloudflare's
    *managed challenge* bot protection. A plain requests.get() (with any
    User-Agent or client-hint headers) gets back an HTTP 429 + `cf-mitigated:
    challenge` HTML "Security challenge" page instead of JSON. The old code did
    `self._get(...).json()`, which then raised JSONDecodeError; crawl() swallowed
    it and returned [] -> every Workable company silently pulled 0 jobs.

    The widget API only exists on `apply.workable.com` (the token-subdomain and
    www hosts return 404 NOT_FOUND for this path), so there is no requests-
    reachable fallback host while the Cloudflare wall is up.

This crawler now:
  * sends browser-like headers (best chance of passing for any board that is
    NOT challenge-walled, and forward-compatible if the policy is relaxed),
  * detects a Cloudflare challenge / non-JSON body explicitly and raises a
    single, clear error so logs say "Cloudflare challenge" rather than a cryptic
    JSONDecodeError,
  * parses the documented widget JSON shape correctly when it IS returned.

Note: some accounts also legitimately expose no public jobs here (empty list).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import requests

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"

# Browser-like headers. The widget endpoint is normally called by the careers
# SPA via XHR, so it expects an Accept of application/json and a same-origin
# Referer. These do NOT defeat a Cloudflare managed challenge on their own (that
# needs a real browser TLS/JS fingerprint), but they maximise the chance for any
# board that is only doing UA-based filtering rather than a full challenge.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"workable\.com/(?:[a-z]+/accounts/)?([A-Za-z0-9_-]+)", s)
    if m and m.group(1) not in ("api", "spi"):
        return m.group(1)
    m2 = re.search(r"([A-Za-z0-9_-]+)\.workable\.com", s)
    if m2:
        return m2.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


class WorkableCrawler(BaseCrawler):
    source_name = "workable"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "workable" or "workable.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        resp = self._get(API.format(token=token), headers=_BROWSER_HEADERS)

        # Cloudflare managed-challenge detection. The challenge comes back as
        # HTTP 200/403/429 with an HTML body and a `cf-mitigated: challenge`
        # header. _get() only raises for HTTP error codes, so a 200-challenge
        # would otherwise slip through to .json() and blow up with a confusing
        # JSONDecodeError. Surface a clear reason instead.
        ctype = resp.headers.get("content-type", "")
        if resp.headers.get("cf-mitigated") == "challenge" or (
            "application/json" not in ctype and "Security challenge" in resp.text[:2000]
        ):
            raise requests.RequestException(
                f"Workable widget for token '{token}' is behind a Cloudflare "
                f"challenge (cf-mitigated={resp.headers.get('cf-mitigated')}, "
                f"content-type={ctype!r}) -- cannot fetch jobs with requests."
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise requests.RequestException(
                f"Workable widget for token '{token}' returned non-JSON "
                f"(content-type={ctype!r}); first bytes: {resp.text[:120]!r}"
            ) from exc

        if not isinstance(data, dict):
            return []
        return data.get("jobs") or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()
        loc = raw.get("location") or {}
        location = ", ".join(
            p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p
        )
        if raw.get("telecommuting") or loc.get("telecommuting"):
            location = (location + " (Remote)").strip()
        job_url = (
            raw.get("url")
            or raw.get("application_url")
            or raw.get("shortlink")
            or ""
        )
        employment_type = (raw.get("employment_type") or "").strip()
        description = truncate(
            clean_html(raw.get("description") or raw.get("requirements") or title)
        )
        return Job(
            company_id=company.id,
            title=title,
            company_name=company.name,
            location=location,
            employment_type=employment_type,
            job_url=job_url,
            source=self.source_name,
            description=description,
            posted_at=parse_date(raw.get("published_on") or raw.get("created_at")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
