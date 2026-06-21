"""
crawlers/recruitee.py
---------------------
Recruitee public offers API (no key needed):
    https://{token}.recruitee.com/api/offers/   -> {"offers": [ ... ]}

`token` is the company subdomain. Accepts a bare token or a *.recruitee.com URL.

Why this file is not just the greenhouse template:
    Recruitee aggressively rate-limits this public endpoint. It answers the
    FIRST request in a window but then returns "429 Too Many Requests" (with an
    HTML body and NO Retry-After header) for rapid follow-ups from the same IP.
    With 17 companies crawled back-to-back the default pipeline got a 429 for
    almost every one, base.crawl() swallowed the HTTPError, and the source
    pulled 0 jobs. It also rejects the default bot User-Agent more readily than
    a browser-like one. So we override fetch_jobs() to send a browser UA and to
    retry a 429 with backoff instead of letting it bubble up as a hard failure.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

import requests

from app.config import settings
from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import parse_date
from app.utils.text import clean_html, make_hash, truncate

# scope=active drops draft/closed offers and is a lighter response.
API = "https://{token}.recruitee.com/api/offers/?scope=active"

# Recruitee 429s the default bot UA much more readily; send a browser UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 429 has no Retry-After; back off manually before giving up.
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8 between attempts


def extract_token(career_url: str) -> str:
    s = (career_url or "").strip().rstrip("/")
    if not s:
        return ""
    m = re.search(r"([A-Za-z0-9_-]+)\.recruitee\.com", s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s:
        return s
    return s.split("/")[-1]


def _pick_localized(raw: Dict[str, Any], field: str) -> str:
    """Recruitee sometimes nulls the top-level html field and only fills the
    per-language copy under translations.{lang}.{field}. Fall back to that."""
    top = raw.get(field)
    if top:
        return top
    translations = raw.get("translations") or {}
    if isinstance(translations, dict):
        # Prefer English, else any language that has the field.
        for lang in ("en", *[k for k in translations if k != "en"]):
            block = translations.get(lang) or {}
            if isinstance(block, dict) and block.get(field):
                return block[field]
    return ""


class RecruiteeCrawler(BaseCrawler):
    source_name = "recruitee"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "recruitee" or "recruitee.com" in s

    def _get_offers(self, url: str) -> requests.Response:
        """GET with the same politeness as base._get but with a browser UA and
        retry/backoff on 429 (Recruitee's public-endpoint throttle)."""
        last_exc: requests.HTTPError | None = None
        for attempt in range(_MAX_RETRIES):
            time.sleep(max(0.0, settings.crawl_delay_seconds))
            resp = self.session.get(
                url,
                timeout=settings.request_timeout_seconds,
                headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            )
            if resp.status_code == 429:
                # No Retry-After is sent; use exponential backoff and retry.
                last_exc = requests.HTTPError("429 Too Many Requests", response=resp)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp
        # Exhausted retries -> raise so base.crawl() logs it as an HTTP error.
        assert last_exc is not None
        raise last_exc

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        token = extract_token(company.career_url)
        if not token:
            return []
        data = self._get_offers(API.format(token=token)).json()
        return data.get("offers", []) or []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        title = (raw.get("title") or "").strip()

        location = (raw.get("location") or "").strip()
        if not location:
            location = ", ".join(
                p for p in [raw.get("city"), raw.get("country")] if p
            ).strip()

        job_url = raw.get("careers_url") or raw.get("careers_apply_url") or ""
        employment_type = (
            raw.get("employment_type_code") or raw.get("category_code") or ""
        ).strip()

        description = truncate(
            clean_html(
                (
                    _pick_localized(raw, "description")
                    + " "
                    + _pick_localized(raw, "requirements")
                ).strip()
            )
            or title
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
            posted_at=parse_date(raw.get("published_at") or raw.get("created_at")),
            raw_data_hash=make_hash(company.name, title, location, job_url),
        )
