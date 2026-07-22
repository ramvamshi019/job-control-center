"""
services/jobright_radar.py
--------------------------
Fully-automatic estimate of whether a job is one that JobRight (jobright.ai)
would ALREADY be showing you — so you can focus on the ones it almost certainly
MISSED and apply there with less competition.

JobRight aggregates from the big boards (LinkedIn / Indeed / well-known company
pages). We don't call JobRight at all (no public API, no login). Instead we infer
its likely coverage from signals we already have:

  1. SOURCE aggregation — how heavily the big boards scrape this ATS. Greenhouse
     / Lever postings are syndicated everywhere (JobRight surely has them). Niche
     ATSes like iCIMS / Workday / SmartRecruiters / BambooHR are scraped far less,
     so JobRight tends to miss them. (Mirrors the dashboard's `competition()`.)
  2. COMPANY prominence — a famous brand, or a company posting a huge number of
     roles, is on everyone's radar (JobRight included), regardless of ATS.
  3. FRESHNESS — a posting JCC found minutes ago, straight from a company board,
     hasn't propagated to the big boards yet, so JobRight hasn't indexed it.

Output tier per job:
  - "exclusive": low-aggregation ATS + not prominent  → JobRight likely MISSED it.
  - "common":    famous / huge-footprint company       → JobRight surely HAS it.
  - "likely":    everything in between (syndicated board, or mid signals).

`exclusivity` is a 0-100 confidence that JobRight missed it (higher = better edge).
Everything here is a pure function of data we already store — no network calls.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

# --- Source buckets -------------------------------------------------------
# Niche ATSes the big boards scrape lightly → JobRight tends to MISS these.
DIRECT_LOW_VIS = {
    "icims", "workday", "smartrecruiters", "bamboohr", "recruitee",
    "workable", "jobvite", "rippling", "gem", "breezy", "eightfold",
}
# Heavily-syndicated ATSes → JobRight almost always HAS these.
HEAVILY_AGGREGATED = {"greenhouse", "lever", "ashby"}
# Sources that are themselves job boards / aggregators → JobRight-like, so it
# very likely shows the same listing.
AGGREGATOR_BOARDS = {
    "himalayas", "themuse", "jobicy", "remoteok", "remotive",
    "arbeitnow", "new_grad", "yc_waas", "hn_hiring",
    "jobspresso", "weworkremotely",
}

# A company with at least this many total postings is a large, well-known
# employer that JobRight certainly tracks (e.g. Boeing, SpaceX, Bosch).
FOOTPRINT_PROMINENT = 250

# Well-known employers JobRight definitely surfaces. Normalized (lowercased,
# alphanumerics only) so it matches the DB's smushed names ("Spacex", "Abbvie").
# Substring match, so "google" also catches "googlecloud", etc.
FAMOUS_BRANDS = {
    "google", "alphabet", "meta", "facebook", "amazon", "aws", "apple",
    "microsoft", "netflix", "nvidia", "tesla", "spacex", "openai",
    "anthropic", "oracle", "salesforce", "adobe", "ibm", "intel", "cisco",
    "uber", "lyft", "airbnb", "stripe", "block", "square", "paypal",
    "snowflake", "databricks", "palantir", "datadog", "atlassian", "shopify",
    "spotify", "linkedin", "twitter", "snap", "pinterest", "reddit", "doordash",
    "instacart", "robinhood", "coinbase", "dropbox", "twilio", "cloudflare",
    "mongodb", "confluent", "hashicorp", "gitlab", "github", "figma", "notion",
    "boeing", "lockheed", "northrop", "raytheon", "anduril", "bosch", "boschgroup",
    "siemens", "ge", "honeywell", "abbvie", "pfizer", "jpmorgan", "goldman",
    "morganstanley", "capitalone", "visa", "mastercard", "walmart", "target",
    "deloitte", "accenture", "pwc", "kpmg", "ey", "mckinsey", "bcg",
    "samsung", "intuit", "workday", "servicenow", "vmware", "dell", "hp",
    "qualcomm", "amd", "broadcom", "tiktok", "bytedance", "bloomberg",
}


def _norm(name: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_prominent(company_name: Optional[str], company_job_count: int = 0,
                 sponsor_score: int = 0) -> bool:
    """A company JobRight certainly tracks: famous brand, huge footprint, or a
    long, well-documented H-1B sponsor (those are large established employers)."""
    if company_job_count >= FOOTPRINT_PROMINENT:
        return True
    if sponsor_score >= 80:
        return True
    norm = _norm(company_name)
    return any(b in norm for b in FAMOUS_BRANDS)


def classify(
    *,
    source: Optional[str],
    company_name: Optional[str],
    company_job_count: int = 0,
    sponsor_score: int = 0,
    discovered_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Return {tier, exclusivity, reason} estimating JobRight coverage."""
    src = (source or "").lower()
    prominent = is_prominent(company_name, company_job_count, sponsor_score)

    if prominent:
        tier, base, why = "common", 8, "well-known employer — on everyone's radar"
    elif src in AGGREGATOR_BOARDS:
        tier, base, why = "likely", 35, f"listed via aggregator board ({src})"
    elif src in HEAVILY_AGGREGATED:
        tier, base, why = "likely", 45, f"{src} is heavily syndicated to big boards"
    elif src in DIRECT_LOW_VIS:
        tier, base, why = "exclusive", 80, f"niche ATS ({src}) the big boards rarely scrape"
    else:
        tier, base, why = "likely", 50, "mixed signals"

    # Freshness: a just-discovered direct posting hasn't propagated yet.
    score = base
    if discovered_at is not None:
        now = now or datetime.utcnow()
        age = now - discovered_at
        if age <= timedelta(hours=24):
            score += 15
            why += " · fresh (<24h, not yet propagated)"
        elif age <= timedelta(hours=72):
            score += 8

    score = max(0, min(100, score))
    return {"jobright_tier": tier, "jobright_exclusivity": score, "jobright_reason": why}


# Source sets we can push into SQL to pre-narrow before per-row classification.
def sources_for_tier(tier: str) -> Optional[set]:
    if tier == "exclusive":
        return set(DIRECT_LOW_VIS)
    if tier == "likely":
        return set(AGGREGATOR_BOARDS) | set(HEAVILY_AGGREGATED)
    return None  # "common" is prominence-driven across all sources
