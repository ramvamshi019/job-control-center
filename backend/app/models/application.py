"""
models/application.py
----------------------
An Application is created when YOU decide to apply (or approve) a job.
It tracks the human side: when applied, follow-up date, which resume version,
and notes. One job -> at most one active application in the MVP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)

    status: str = Field(default="Approved", index=True)
    applied_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    resume_version: str = Field(default="", description="e.g. base_data_engineer")
    notes: str = Field(default="")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
