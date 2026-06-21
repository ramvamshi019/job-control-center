"""
routes/applications.py
----------------------
Track the human side of applying: create an application from an approved job,
record applied/follow-up dates, resume version, and notes.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.models.application import Application
from app.models.job import Job

router = APIRouter(prefix="/applications", tags=["applications"])


class ApplicationCreate(BaseModel):
    job_id: int
    status: str = "Approved"
    resume_version: str = ""
    notes: str = ""


class ApplicationUpdate(BaseModel):
    status: Optional[str] = None
    applied_at: Optional[datetime] = None
    follow_up_at: Optional[datetime] = None
    resume_version: Optional[str] = None
    notes: Optional[str] = None


@router.get("/", response_model=List[Application])
def list_applications(session: Session = Depends(get_session)):
    return session.exec(select(Application).order_by(Application.updated_at.desc())).all()


@router.post("/", response_model=Application)
def create_application(payload: ApplicationCreate, session: Session = Depends(get_session)):
    job = session.get(Job, payload.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    app_row = Application(**payload.model_dump())
    session.add(app_row)
    # Keep the job's status in sync.
    job.status = payload.status if payload.status in ("Approved", "Applied") else job.status
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(app_row)
    return app_row


@router.patch("/{application_id}", response_model=Application)
def update_application(application_id: int, payload: ApplicationUpdate, session: Session = Depends(get_session)):
    app_row = session.get(Application, application_id)
    if not app_row:
        raise HTTPException(404, "Application not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(app_row, key, value)
    app_row.updated_at = datetime.utcnow()
    session.add(app_row)

    # Mirror status onto the job so the dashboard stays consistent.
    if payload.status:
        job = session.get(Job, app_row.job_id)
        if job:
            job.status = payload.status
            session.add(job)
    session.commit()
    session.refresh(app_row)
    return app_row
