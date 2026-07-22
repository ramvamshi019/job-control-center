"""
crawlers/registry.py
---------------------
Holds the list of available crawlers and picks the right one for a company.

To add a new source later (Phase 2+), just:
  1. create crawlers/<name>.py with a BaseCrawler subclass
  2. import it here and add an instance to CRAWLERS

`get_crawler_for(company)` returns the first crawler whose can_handle() is True,
matching on ats_type first, then on the career_url.
"""

from __future__ import annotations

from typing import List, Optional

from app.crawlers.base import BaseCrawler
from app.crawlers.greenhouse import GreenhouseCrawler
from app.crawlers.lever import LeverCrawler
from app.crawlers.ashby import AshbyCrawler
from app.crawlers.smartrecruiters import SmartRecruitersCrawler
from app.crawlers.bamboohr import BambooHRCrawler
from app.crawlers.workable import WorkableCrawler
from app.crawlers.recruitee import RecruiteeCrawler
from app.crawlers.workday import WorkdayCrawler
from app.crawlers.icims import IcimsCrawler
from app.crawlers.rippling import RipplingCrawler
from app.crawlers.hn_hiring import HNHiringCrawler
from app.crawlers.gem import GemCrawler
from app.crawlers.himalayas import HimalayasCrawler
from app.crawlers.eightfold import EightfoldCrawler
from app.crawlers.jobvite import JobviteCrawler
from app.crawlers.yc_waas import YCWaaSCrawler
from app.crawlers.remotive import RemotiveCrawler
from app.crawlers.new_grad import NewGradCrawler
from app.crawlers.breezy import BreezyCrawler
from app.crawlers.paylocity import PaylocityCrawler
from app.crawlers.ukg import UKGCrawler
from app.crawlers.oracle_hcm import OracleHCMCrawler
from app.crawlers.themuse import TheMuseCrawler
from app.crawlers.jobicy import JobicyCrawler
from app.crawlers.remoteok import RemoteOKCrawler
from app.crawlers.arbeitnow import ArbeitnowCrawler
from app.crawlers.jobspresso import JobspressoCrawler
from app.crawlers.weworkremotely import WeWorkRemotelyCrawler
from app.crawlers.generic_placeholder import GenericCrawler
from app.models.company import Company
from app.utils.logging import get_logger

log = get_logger("registry")

# ---- Real crawlers: all share clean public token-based JSON APIs. ----
CRAWLERS: List[BaseCrawler] = [
    GreenhouseCrawler(),
    LeverCrawler(),
    AshbyCrawler(),
    SmartRecruitersCrawler(),
    BambooHRCrawler(),
    WorkableCrawler(),
    RecruiteeCrawler(),
    WorkdayCrawler(),
    IcimsCrawler(),
    RipplingCrawler(),
    HNHiringCrawler(),    # sentinel source: HN "Who is hiring" thread
    GemCrawler(),
    HimalayasCrawler(),   # sentinel source: Himalayas remote board
    EightfoldCrawler(),   # enterprise/Fortune-500 boards (per-company slug|domain)
    JobviteCrawler(),
    YCWaaSCrawler(),      # sentinel source: YC Work at a Startup
    RemotiveCrawler(),    # sentinel source: Remotive remote board
    NewGradCrawler(),     # sentinel: GitHub new-grad/internship lists (entry-level)
    BreezyCrawler(),      # per-company Breezy HR boards
    PaylocityCrawler(),   # per-company Paylocity Recruiting boards (US SMB/mid-market)
    UKGCrawler(),         # UKG Pro / UltiPro boards (host|code|board-guid)
    OracleHCMCrawler(),   # Oracle Cloud HCM "ORC" career sites (host|siteNumber)
    TheMuseCrawler(),     # sentinel source: The Muse
    JobicyCrawler(),      # sentinel source: Jobicy remote
    RemoteOKCrawler(),    # sentinel source: RemoteOK
    ArbeitnowCrawler(),   # sentinel source: Arbeitnow (EU-heavy; ~0 US currently)
    JobspressoCrawler(),      # sentinel source: Jobspresso RSS (WP Job Manager)
    WeWorkRemotelyCrawler(),  # sentinel source: We Work Remotely RSS
    GenericCrawler(),  # keep LAST: matches anything as a fallback.
]


def get_crawler_for(company: Company) -> Optional[BaseCrawler]:
    """Pick a crawler by ats_type, falling back to career_url matching."""
    ats = (company.ats_type or "").strip().lower()
    for crawler in CRAWLERS:
        if crawler.can_handle(ats):
            return crawler
    for crawler in CRAWLERS:
        if crawler.can_handle(company.career_url or ""):
            return crawler
    log.warning("No crawler found for company '%s' (ats=%s)", company.name, company.ats_type)
    return None


def list_sources() -> List[str]:
    return [c.source_name for c in CRAWLERS]
