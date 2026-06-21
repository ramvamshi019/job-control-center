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

from datetime import datetime
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
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    raw_data_hash: str = Field(default="", index=True, description="dedupe key")

    # ---- Computed by the engines ----
    match_score: int = Field(default=0, index=True)
    sponsorship_risk: str = Field(default="unknown", index=True)
    status: str = Field(default="New", index=True)

    rejection_reason: str = Field(default="")
    fit_reason: str = Field(default="")
    risk_reason: str = Field(default="")
    resume_notes: str = Field(default="")
    cover_letter: str = Field(default="")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
