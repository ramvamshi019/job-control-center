"""
routes/applications.py
----------------------
Track the human side of applying: create an application from an approved job,
record applied/follow-up dates, resume version, and notes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.database import get_session
from app.models.application import Application
from app.models.company import Company
from app.models.job import Job
from app.utils.dates import utcnow_naive
from app.utils.text import make_hash

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
    job.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(job)
    session.commit()
    session.refresh(app_row)
    return app_row


class ExternalApplyIn(BaseModel):
    url: str
    title: Optional[str] = None
    company: Optional[str] = None
    source: Optional[str] = None  # 'dashboard' or 'chrome-extension'


def _normalize_url(u: str) -> str:
    """Strip tracking params + fragments + trailing slashes for URL matching.
    Keeps the path and known ATS-meaningful query params (?gh_jid, ?jobId, etc).
    Coarse — enough to catch 'same job with a UTM tag' as a match."""
    try:
        p = urlparse(u.strip())
        # keep only the meaningful query — filter utm_* / gclid / mc_cid / fbclid noise
        q = "&".join(
            kv for kv in (p.query or "").split("&")
            if kv and not kv.lower().startswith(("utm_", "gclid=", "mc_cid=", "fbclid=", "ref="))
        )
        norm = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"
        if q:
            norm += "?" + q
        return norm.lower()
    except Exception:
        return u.strip().lower()


@router.post("/log-external")
def log_external_apply(payload: ExternalApplyIn, session: Session = Depends(get_session)):
    """Log a job application submitted OUTSIDE the JCC flow (LinkedIn Easy
    Apply, direct visit to a company site, Chrome extension autofire on form
    submit, etc). Two-tier match:

      1. Fuzzy URL match against known jobs.job_url — flip that row to
         'Applied'.
      2. If no match, insert a NEW job row with status='Applied' so it
         shows up on the Applied page for tracking / follow-up.

    Never inserts a duplicate: matching uses a normalized URL so
    ?utm_source=linkedin doesn't create a second copy.
    """
    if not payload.url:
        raise HTTPException(400, "url required")

    norm = _normalize_url(payload.url)
    if not norm.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")

    # 1. Match by normalized URL. Coarse LIKE on stored job_url so a query
    #    param drift doesn't miss a match.
    #    Strategy: exact match on our-side normalized form, then fallback to
    #    stripped-scheme LIKE.
    stripped = norm.split("://", 1)[-1].split("?", 1)[0]
    existing = session.exec(
        select(Job).where(func.lower(Job.job_url).like(f"%{stripped}%"))
    ).first()

    now = utcnow_naive()
    if existing:
        was = existing.status
        existing.status = "Applied"
        existing.updated_at = now
        session.add(existing)
        session.commit()
        return {
            "matched": True, "action": "updated",
            "job_id": existing.id, "was_status": was, "new_status": "Applied",
            "title": existing.title, "company_name": existing.company_name,
        }

    # 2. No match — insert as a new Applied row. Company row is optional; we
    #    stash the domain in company_name so the Applied page has something
    #    to render even without a full crawl.
    host = urlparse(payload.url).netloc.lower().replace("www.", "")
    company_name = (payload.company or host or "External").strip()[:120]
    title = (payload.title or f"[external] {host}").strip()[:200]

    # Look up or create a placeholder company for grouping
    company = session.exec(
        select(Company).where(func.lower(Company.name) == company_name.lower())
    ).first()
    if not company:
        company = Company(
            name=company_name, ats_type="external",
            career_url=host, is_active=False, priority="low",
            h1b_history_score=0, notes="[auto-created from log-external]",
            created_at=now, updated_at=now,
        )
        session.add(company)
        session.commit()
        session.refresh(company)

    job = Job(
        company_id=company.id,
        title=title,
        company_name=company_name,
        location="",
        employment_type="Full-time",
        job_url=payload.url,
        source=payload.source or "external",
        description=f"[Logged externally via {payload.source or 'dashboard'}]",
        raw_data_hash=make_hash(company_name, title, "", payload.url),
        match_score=0,
        sponsorship_risk="unknown",
        status="Applied",
        rejection_reason="",
        fit_reason="",
        risk_reason="",
        resume_notes="",
        cover_letter="",
        discovered_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return {
        "matched": False, "action": "inserted",
        "job_id": job.id, "new_status": "Applied",
        "title": title, "company_name": company_name,
    }


@router.patch("/{application_id}", response_model=Application)
def update_application(application_id: int, payload: ApplicationUpdate, session: Session = Depends(get_session)):
    app_row = session.get(Application, application_id)
    if not app_row:
        raise HTTPException(404, "Application not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(app_row, key, value)
    app_row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
