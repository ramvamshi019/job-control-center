"""
routes/companies.py
-------------------
CRUD for companies + a trigger to crawl one company on demand.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.models.company import Company
from app.services import scheduler

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
    company.updated_at = datetime.utcnow()
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
