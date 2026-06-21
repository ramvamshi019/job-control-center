"""
crawlers/generic_placeholder.py
-------------------------------
Fallback crawler. Registered LAST in the registry so it only matches when no
specialized crawler does. It does NOT scrape arbitrary HTML in the MVP (that is
fragile and risks violating site rules) — it simply logs and returns [].

Later you can extend this into a careful, robots-aware generic scraper, or use
it to route to Phase 4/5 discovery sources.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("crawler.generic")


class GenericCrawler(BaseCrawler):
    source_name = "generic"

    def can_handle(self, url_or_ats: str) -> bool:
        # Matches anything — that's why it must be registered LAST.
        return True

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        log.info(
            "No specialized crawler for %s (ats=%s). Generic fallback returns nothing.",
            company.name,
            company.ats_type,
        )
        return []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:  # pragma: no cover
        raise NotImplementedError("Generic crawler does not normalize in the MVP.")
