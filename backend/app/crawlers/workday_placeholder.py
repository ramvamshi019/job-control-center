"""
crawlers/workday_placeholder.py
-------------------------------
PHASE 2 placeholder. Workday is harder: each tenant has a different host and
the listings come from a POST to a CXS endpoint like:
    https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

This stub returns [] so it can be registered safely today. Implement fetch_jobs
when you reach Phase 2 (see README "Future pro roadmap").
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("crawler.workday")


class WorkdayCrawler(BaseCrawler):
    source_name = "workday"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "workday" or "myworkdayjobs.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        log.info("Workday crawler is a Phase 2 placeholder — skipping %s", company.name)
        return []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:  # pragma: no cover
        raise NotImplementedError("Workday normalize_job not implemented yet (Phase 2).")
