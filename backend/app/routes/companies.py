"""
routes/companies.py
-------------------
CRUD for companies + a trigger to crawl one company on demand.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.models.company import Company
from app.models.job import Job
from app.services import filter_engine, scheduler

router = APIRouter(prefix="/companies", tags=["companies"])


class CompanyCreate(BaseModel):
    name: str
    career_url: str
    ats_type: str
    h1b_history_score: int = 0
    priority: str = "medium"
    is_active: bool = True
    notes: str = ""


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    career_url: Optional[str] = None
    ats_type: Optional[str] = None
    h1b_history_score: Optional[int] = None
    priority: Optional[str] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


@router.get("/", response_model=List[Company])
def list_companies(session: Session = Depends(get_session)):
    return session.exec(select(Company).order_by(Company.name)).all()


@router.post("/", response_model=Company)
def create_company(payload: CompanyCreate, session: Session = Depends(get_session)):
    company = Company(**payload.model_dump())
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


@router.patch("/{company_id}", response_model=Company)
def update_company(company_id: int, payload: CompanyUpdate, session: Session = Depends(get_session)):
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, key, value)
    company.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(company)
    session.commit()
    session.refresh(company)
    return company


@router.post("/{company_id}/crawl")
def crawl_company(company_id: int, session: Session = Depends(get_session)):
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    summary = scheduler.process_company(session, company)
    return summary


class BlockPayload(BaseModel):
    name: str


@router.post("/block")
def block_company(payload: BlockPayload, session: Session = Depends(get_session)):
    """One-click blocklist add + re-evaluate every stored job for that
    company. Appends the company name to data/company_blocklist.txt (safe:
    the file's own dedupe key ignores case + non-alphanumerics) and flips
    all its currently-visible jobs to Rejected. Fully reversible -- delete
    the line from the file and re-run the reeval script.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "name required")

    # Append to the blocklist file. `_squash` in filter_engine collapses
    # case + punctuation, so raw name is fine here; no need to pre-normalize.
    blocklist_path = Path(
        os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "data",
            "company_blocklist.txt",
        ))
    )
    # Deduplicate write: if the squashed form is already present, skip.
    squashed = filter_engine._squash(name)
    already = squashed in filter_engine.blocked_companies()
    if not already:
        with blocklist_path.open("a") as fh:
            fh.write(f"\n# added via dashboard {datetime.now(timezone.utc).replace(tzinfo=None):%Y-%m-%d}\n{name}\n")
        # Bust the in-process cache so the immediate reeval uses the new list.
        filter_engine._blocklist_cache = None
        _ = filter_engine.blocked_companies()

    # Re-evaluate visible jobs for this company.
    jobs = session.exec(
        select(Job).where(
            Job.company_name == name,
            Job.status.in_(["New", "Need Review"]),
        )
    ).all()
    flipped = 0
    for j in jobs:
        result = filter_engine.evaluate(j)
        if not result.passed:
            j.status = "Rejected"
            j.rejection_reason = result.reason
            j.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(j)
            flipped += 1
    session.commit()

    return {
        "name": name,
        "already_blocklisted": already,
        "jobs_reevaluated": len(jobs),
        "jobs_flipped_to_rejected": flipped,
    }
