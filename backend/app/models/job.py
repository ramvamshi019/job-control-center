"""
models/job.py
-------------
A Job is one posting discovered from a source. Every crawler normalizes its
raw output into THIS shape, so the rest of the system never cares where a job
came from.

`raw_data_hash` is used for de-duplication.
`status` is one of the JOB_STATUSES below.
"""

from __future__ import annotations

from datetime import datetime, timezone
from app.utils.dates import utcnow_naive
from typing import Optional

from sqlmodel import Field, SQLModel

# The lifecycle states a job can be in.
JOB_STATUSES = [
    "New",
    "Need Review",
    "Approved",
    "Applied",
    "Follow-up",
    "Rejected",
    "Archived",
]

SPONSORSHIP_RISKS = ["low", "medium", "high", "reject", "unknown"]


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: Optional[int] = Field(default=None, foreign_key="companies.id", index=True)

    # ---- Standardized fields every crawler must fill ----
    title: str = Field(index=True)
    company_name: str = Field(index=True)
    location: str = Field(default="")
    employment_type: str = Field(default="", description="full-time|contract|intern|...")
    job_url: str = Field(default="")
    source: str = Field(default="", index=True, description="crawler/source name")
    description: str = Field(default="")
    posted_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=utcnow_naive)

    # Last time this posting was still present on the employer's board. Stamped
    # on EVERY crawl that re-sees it, unlike discovered_at which is write-once.
    # This is what makes ghost jobs detectable: a filled/closed req simply stops
    # appearing, and age alone can't tell you that — 43% of stored jobs have no
    # posted_at at all (iCIMS, SmartRecruiters), so date-based retention can
    # never expire them.
    last_seen_at: Optional[datetime] = Field(default_factory=utcnow_naive, index=True)
    raw_data_hash: str = Field(default="", index=True, description="dedupe key (source-scoped)")

    # Cross-source dedupe key. Two rows with the same canonical_key are the same
    # real posting seen via different feeds (e.g. Stripe SWE on Greenhouse AND
    # Simplify AND HN Hiring). Computed from normalized (company, title, primary
    # location) — see utils.text.canonical_job_key. Populated by BaseCrawler.crawl()
    # after normalize_job(); crawlers do NOT need to set it themselves.
    canonical_key: str = Field(default="", index=True, description="cross-source dedupe key")

    # Role cluster: ml_ai | data_eng | bi_analytics | cloud_devops | security |
    # backend | fullstack | other. Assigned deterministically from title+desc via
    # app.services.cluster.classify(). Used to pick the right resume variant when
    # auto-tailoring and to group Best Matches by role type (context-switching
    # across clusters is the hidden cost of "just apply to more").
    cluster: str = Field(default="", index=True, description="role cluster slug")

    # ---- Computed by the engines ----
    match_score: int = Field(default=0, index=True)
    sponsorship_risk: str = Field(default="unknown", index=True)
    status: str = Field(default="New", index=True)

    rejection_reason: str = Field(default="")
    fit_reason: str = Field(default="")
    risk_reason: str = Field(default="")
    resume_notes: str = Field(default="")
    cover_letter: str = Field(default="")

    created_at: datetime = Field(default_factory=utcnow_naive)
    updated_at: datetime = Field(default_factory=utcnow_naive)
