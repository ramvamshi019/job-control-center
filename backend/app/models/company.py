"""
models/company.py
-----------------
A Company is a single career portal we know how to crawl.

`career_url` can be a full URL OR a bare ATS token, e.g. for Greenhouse both
"https://boards.greenhouse.io/stripe" and "stripe" work — the crawler extracts
the token either way.

`ats_type` decides which crawler runs: greenhouse | lever | ashby | workday | ...
`priority`: high | medium | low | skip   (drives the scheduler)
`h1b_history_score`: 0-100 rough confidence the company sponsors visas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(index=True)
    career_url: str = Field(description="Full board URL or bare ATS token")
    ats_type: str = Field(index=True, description="greenhouse|lever|ashby|...")

    h1b_history_score: int = Field(default=0, description="0-100 sponsorship confidence")
    priority: str = Field(default="medium", description="high|medium|low|skip")

    last_checked_at: Optional[datetime] = None
    is_active: bool = Field(default=True, index=True)
    notes: str = Field(default="")

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
