"""
crawlers/base.py
----------------
The pluggable crawler interface. EVERY source (Greenhouse, Lever, Ashby, and
all future ones) subclasses BaseCrawler and implements the same 4 methods.

Contract:
    source_name           -> short string id, e.g. "greenhouse"
    can_handle(url/ats)    -> True if this crawler owns that company
    fetch_jobs(company)    -> list[RawJob-ish dicts] from the network
    normalize_job(raw, co) -> a standardized Job (NOT yet scored/saved)

`crawl(company)` is the public entry point: it fetches, normalizes, and returns
a list of Job objects. It never raises — errors are logged and an empty list is
returned, so one broken company can't stop a whole crawl run.
"""

from __future__ import annotations

import abc
import threading
import time
from typing import Any, Dict, List

import requests

from app.config import settings
from app.models.company import Company
from app.models.job import Job
from app.utils.logging import get_logger

log = get_logger("crawler")


class BaseCrawler(abc.ABC):
    #: Subclasses MUST set this.
    source_name: str = "base"

    def __init__(self) -> None:
        # Crawlers are shared singletons (registry.CRAWLERS), but the live
        # watcher fans each one out across many worker threads. requests.Session
        # is NOT thread-safe (shared connection pool -> "provisioning a new
        # connection" races), so every thread gets its OWN session via
        # thread-local storage instead of sharing one.
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": settings.user_agent})
            self._local.session = s
        return s

    # ---- interface methods ----
    @abc.abstractmethod
    def can_handle(self, url_or_ats: str) -> bool:
        """Return True if this crawler should handle the given URL / ats_type."""

    @abc.abstractmethod
    def fetch_jobs(self, company: Company) -> List[Dict[str, Any]]:
        """Hit the network and return a list of raw job dicts."""

    @abc.abstractmethod
    def normalize_job(self, raw: Dict[str, Any], company: Company) -> Job:
        """Convert one raw job dict into a standardized Job object."""

    # ---- shared helpers ----
    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET with timeout + polite delay + error raising."""
        time.sleep(max(0.0, settings.crawl_delay_seconds))
        resp = self.session.get(url, timeout=settings.request_timeout_seconds, **kwargs)
        resp.raise_for_status()
        return resp

    def crawl(self, company: Company) -> List[Job]:
        """Public entry point. Safe: logs and returns [] on any failure."""
        try:
            raw_jobs = self.fetch_jobs(company)
        except requests.HTTPError as exc:
            log.warning("[%s] HTTP error for %s: %s", self.source_name, company.name, exc)
            return []
        except requests.RequestException as exc:
            log.warning("[%s] network error for %s: %s", self.source_name, company.name, exc)
            return []
        except Exception as exc:  # noqa: BLE001 - never let one company kill the run
            log.exception("[%s] unexpected fetch error for %s: %s", self.source_name, company.name, exc)
            return []

        jobs: List[Job] = []
        for raw in raw_jobs:
            try:
                jobs.append(self.normalize_job(raw, company))
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not normalize a job for %s: %s", self.source_name, company.name, exc)
        log.info("[%s] %s -> %d jobs", self.source_name, company.name, len(jobs))
        return jobs
