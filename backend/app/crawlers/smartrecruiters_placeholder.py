"""
crawlers/smartrecruiters_placeholder.py
---------------------------------------
PHASE 2 placeholder. SmartRecruiters HAS a clean public API:
    https://api.smartrecruiters.com/v1/companies/{token}/postings
This stub is registered now and returns []. Implementing it later is mostly
filling in fetch_jobs + normalize_job (it's one of the easiest Phase 2 sources).
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.crawlers.base import BaseCrawler
from app.models.company import Company
from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("crawler.smartrecruiters")


class SmartRecruitersCrawler(BaseCrawler):
    source_name = "smartrecruiters"

    def can_handle(self, url_or_ats: str) -> bool:
        s = (url_or_ats or "").lower()
        return s == "smartrecruiters" or "smartrecruiters.com" in s

    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        log.info("SmartRecruiters crawler is a Phase 2 placeholder — skipping %s", company.name)
        return []

    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:  # pragma: no cover
        raise NotImplementedError("SmartRecruiters normalize_job not implemented yet (Phase 2).")
