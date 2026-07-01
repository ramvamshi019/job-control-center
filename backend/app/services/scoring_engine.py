"""
services/scoring_engine.py
--------------------------
Scores a job 0-100 for an F-1/OPT entry-level candidate.

`score(job, company)` returns (score:int, fit_reason:str).
The fit_reason is a human-readable breakdown shown in the dashboard.

This runs AFTER hard filters (rejected jobs aren't scored). It still applies
negative signals so borderline jobs land in "Need Review" rather than "Best".
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from app.config import settings
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import hours_since
from app.utils.text import normalize, term_in

# Clearly data-engineering titles. The candidate is a Data Engineer first, so
# these get an extra boost over a generic "software engineer" match — otherwise
# unrelated SWE roles outrank the data jobs he actually wants.
DATA_ROLE_SIGNALS = [
    "data engineer", "data engineering", "etl", "analytics engineer",
    "big data", "data platform", "data warehouse", "data pipeline",
    "data infrastructure", "ml engineer", "machine learning engineer",
    "data developer", "bi engineer",
]

# Data/backend signals that SPARE a title from the off-domain penalty — so a
# "Mobile Data Engineer" or "Backend Platform Engineer" stays in your lane, but a
# bare "Frontend Software Engineer" does not (generic "software engineer" is NOT
# a sparing signal on purpose).
BACKEND_SPARING = DATA_ROLE_SIGNALS + [
    "backend", "back end", "back-end", "platform", "cloud",
    "infrastructure", "devops", "database",
]

# Off-domain SPECIALTIES that aren't a Python/SQL/Spark data-or-backend fit. These
# pass the tech-title gate but should rank well below your real lane (de-ranked,
# not excluded, so a "Full Stack" role with backend still survives).
OFF_DOMAIN_SIGNALS = [
    "front end", "frontend", "front-end", "ui engineer", "ux engineer",
    "ios", "android", "mobile", "game", "gameplay", "embedded", "firmware",
    "hardware", "fpga", "asic", "mechanical", "electrical", "rf engineer",
]

# Your CORE stack — weighted higher than generic skills so a real data/backend
# match outranks a role that merely name-drops "git"/"docker".
CORE_SKILLS = [
    "python", "sql", "pl/sql", "spark", "pyspark", "spark sql", "spark streaming",
    "hadoop", "hive", "hdfs", "scala", "etl", "elt", "airflow", "kafka",
    "snowflake", "databricks", "redshift", "bigquery", "dbt", "pandas",
    "postgresql", "data warehouse", "data pipeline",
]

JUNIOR_SIGNALS = [
    "junior", "entry level", "entry-level", "new grad", "new graduate",
    "recent graduate", "associate", "early career",
    "engineer i", "developer i", "0-2 years", "0 to 2 years", "1+ years",
]
SENIOR_SIGNALS = ["senior", "sr.", "staff", "principal", "lead", "manager", "director", "architect"]
CLEARANCE_SIGNALS = ["clearance", "citizen", "ts/sci"]
NO_SPONSOR_SIGNALS = ["no visa sponsorship", "we do not sponsor", "without sponsorship", "unable to sponsor"]
CONTRACT_SIGNALS = ["c2c", "corp to corp", "contract", "1099"]
SPAM_SIGNALS = ["staffing", "consulting", "consultancy", "recruiting agency", "client of", "our client"]
# Word-boundary tokens (see term_in). "remote" is handled separately, so the
# "remote - us" variants are unnecessary here.
US_LOCATION_SIGNALS = [
    "united states", "usa", "u.s.", "us",
    "ca", "ny", "tx", "wa", "ma", "il", "ga", "nj", "va",
]


def _contains_any(text: str, words: List[str]) -> bool:
    return any(w in text for w in words)


def _role_match(title: str) -> bool:
    return _contains_any(title, settings.target_roles_list) or _contains_any(
        title,
        ["data engineer", "cloud engineer", "software engineer", "developer",
         "etl", "backend", "back end", "back-end", "platform engineer",
         "data analyst", "analytics engineer"],
    )


def _skills_matched(desc: str) -> List[str]:
    return [s for s in settings.skills_list if s and term_in(desc, s)]


def _years_required(desc: str) -> Optional[int]:
    """Find the smallest 'N years' figure mentioned, or None."""
    nums = [int(n) for n in re.findall(r"(\d{1,2})\+?\s*years", desc)]
    return min(nums) if nums else None


def score(job: Job, company: Optional[Company] = None) -> Tuple[int, str]:
    title = normalize(job.title)
    desc = normalize(job.description)
    loc = normalize(job.location)
    etype = normalize(job.employment_type)
    reasons: List[str] = []
    total = 0

    # ---- Positive signals ----
    if _role_match(title):
        total += 20
        reasons.append("+20 target role match")

    # Data-engineering titles are the candidate's PRIMARY fit — boost them so
    # they outrank generic SWE roles in the "Best" list.
    if _contains_any(title, DATA_ROLE_SIGNALS):
        total += 15
        reasons.append("+15 data-engineering role")

    # A core skill named right in the TITLE (e.g. "Data Engineer - Spark") is a
    # much stronger fit signal than the same word buried in the description.
    title_skills = [s for s in settings.skills_list if s and term_in(title, s)]
    if title_skills:
        total += 6
        reasons.append(f"+6 skill in title ({', '.join(title_skills[:3])})")

    if "full" in etype or ("full-time" in desc and not etype):
        total += 15
        reasons.append("+15 full-time")
    elif not etype:
        # Unknown type — most crawled postings just don't state it, so give a
        # partial benefit of the doubt instead of nothing (was 0, which sank
        # otherwise-strong data jobs below the New threshold).
        total += 8
        reasons.append("+8 employment type unknown (assumed full-time)")

    if _contains_any(title, JUNIOR_SIGNALS) or _contains_any(desc, JUNIOR_SIGNALS):
        total += 15
        reasons.append("+15 junior/entry signal")

    # Count-weighted skills, with your CORE data/backend stack worth more (+5)
    # than generic tokens (+3). A role naming NONE of your skills loses points
    # instead of riding a bare title match.
    matched = _skills_matched(desc)
    if matched:
        core = [s for s in matched if s in CORE_SKILLS]
        other = [s for s in matched if s not in CORE_SKILLS]
        pts = min(30, 5 * len(core) + 3 * len(other))
        total += pts
        reasons.append(
            f"+{pts} skills x{len(matched)} ({len(core)} core: {', '.join((core or other)[:5])})")
    else:
        total -= 8
        reasons.append("-8 no overlap with your skill stack")

    hrs = hours_since(job.posted_at)
    if hrs is not None and hrs <= 72:
        total += 10
        reasons.append("+10 posted within 72h")

    if company and company.h1b_history_score >= 50:
        total += 10
        reasons.append("+10 company has sponsorship history")

    # Word-boundary match: bare state codes like "ca"/"ny" must not substring-hit
    # "scalable"/"company". (term_in treats non-alphanumerics as boundaries.)
    if any(term_in(loc, s) for s in US_LOCATION_SIGNALS) or "remote" in loc:
        total += 10
        reasons.append("+10 US / remote-US location")

    if not _contains_any(title + " " + desc, SPAM_SIGNALS):
        total += 5
        reasons.append("+5 not staffing/consulting spam")

    # ---- Negative signals ----
    # Penalize companies with no confirmed H-1B history *unless* the posting
    # itself signals no-sponsorship (that's handled separately below at -50).
    # Confirmed sponsors (enriched score >= 50) skip this and also get the +10
    # bonus above, so they clearly outrank unknown-sponsorship companies.
    sponsor_history = bool(company and company.h1b_history_score >= 50)
    if not sponsor_history and not _contains_any(desc, NO_SPONSOR_SIGNALS):
        # Softened from -30: nearly every company has unknown H-1B history, so a
        # big penalty was noise that buried real data jobs while big-name
        # sponsors skipped it. Explicit "no sponsorship" is still -50 below.
        total -= 12
        reasons.append("-12 sponsorship unclear & no known history")

    if _contains_any(title, SENIOR_SIGNALS):
        total -= 40
        reasons.append("-40 looks mid/senior")

    # Off-domain specialty (frontend / mobile / embedded / hardware …) that isn't
    # a data-or-backend fit — de-rank (not exclude) unless the title also names a
    # data/backend/cloud signal.
    if _contains_any(title, OFF_DOMAIN_SIGNALS) and not _contains_any(title, BACKEND_SPARING):
        total -= 20
        reasons.append("-20 off-domain specialty (not data/backend)")

    if _contains_any(desc, CLEARANCE_SIGNALS):
        total -= 50
        reasons.append("-50 clearance/citizenship language")

    if _contains_any(desc, NO_SPONSOR_SIGNALS):
        total -= 50
        reasons.append("-50 no-sponsorship language")

    if _contains_any(etype + " " + desc, CONTRACT_SIGNALS):
        total -= 25
        reasons.append("-25 contract/C2C signal")

    years = _years_required(desc)
    if years is not None:
        if years >= 5:
            total -= 40
            reasons.append(f"-40 requires {years}+ years")
        elif years >= 3:
            total -= 20
            reasons.append(f"-20 requires {years}+ years")

    # Clamp to 0-100.
    total = max(0, min(100, total))
    return total, " | ".join(reasons)
