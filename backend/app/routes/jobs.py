"""
routes/jobs.py
--------------
Read jobs (with filters) and update a job's status / reason / materials.
The dashboard talks to these endpoints.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, or_, select

from app.config import settings
from app.database import get_session
from app.models.job import JOB_STATUSES, Job
from app.services import cover_letter as cover_letter_service
from app.services import resume_builder as resume_builder_service
from app.services import resume_tailor as resume_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobStatusUpdate(BaseModel):
    status: Optional[str] = None
    rejection_reason: Optional[str] = None
    resume_notes: Optional[str] = None
    cover_letter: Optional[str] = None


@router.get("/", response_model=List[Job])
def list_jobs(
    session: Session = Depends(get_session),
    status: Optional[str] = Query(None, description="Filter by status"),
    min_score: int = Query(0, description="Minimum match score"),
    sponsorship_risk: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Search title / company / location"),
    posted_within_hours: Optional[int] = Query(
        None, description="Only jobs POSTED within the last N hours"),
    discovered_within_hours: Optional[int] = Query(
        None, description="Only jobs first SEEN by the crawler within the last N hours"),
    exclude_rejected: bool = Query(False, description="Hide Rejected jobs (US-only survivors)"),
    order_by: str = Query("score", description="score | posted | discovered"),
    limit: int = Query(200, le=1000),
):
    stmt = select(Job).where(Job.match_score >= min_score)
    if status:
        stmt = stmt.where(Job.status == status)
    if exclude_rejected:
        stmt = stmt.where(Job.status != "Rejected")
    if sponsorship_risk:
        stmt = stmt.where(Job.sponsorship_risk == sponsorship_risk)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(Job.title.ilike(like), Job.company_name.ilike(like), Job.location.ilike(like))
        )
    if posted_within_hours:
        cutoff = datetime.utcnow() - timedelta(hours=posted_within_hours)
        stmt = stmt.where(Job.posted_at >= cutoff)
    if discovered_within_hours:
        cutoff = datetime.utcnow() - timedelta(hours=discovered_within_hours)
        stmt = stmt.where(Job.discovered_at >= cutoff)

    if order_by == "posted":
        stmt = stmt.order_by(Job.posted_at.desc())
    elif order_by == "discovered":
        stmt = stmt.order_by(Job.discovered_at.desc())
    else:
        stmt = stmt.order_by(Job.match_score.desc())
    stmt = stmt.limit(limit)
    return session.exec(stmt).all()


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: int, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.patch("/{job_id}", response_model=Job)
def update_job(job_id: int, payload: JobStatusUpdate, session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if payload.status is not None:
        if payload.status not in JOB_STATUSES:
            raise HTTPException(400, f"Invalid status. Use one of {JOB_STATUSES}")
        job.status = payload.status
    if payload.rejection_reason is not None:
        job.rejection_reason = payload.rejection_reason
    if payload.resume_notes is not None:
        job.resume_notes = payload.resume_notes
    if payload.cover_letter is not None:
        job.cover_letter = payload.cover_letter
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.post("/{job_id}/regenerate", response_model=Job)
def regenerate_materials(
    job_id: int,
    include_opt: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Re-create resume notes + cover letter for a single job on demand."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.resume_notes = resume_service.generate(job)
    job.cover_letter = cover_letter_service.generate(job, include_opt=include_opt)
    job.updated_at = datetime.utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.post("/{job_id}/build-resume")
def build_resume(job_id: int, session: Session = Depends(get_session)):
    """Build a full, honestly-tailored résumé for this job (Claude if a key is
    set, else the untouched base résumé) and save it to resumes/generated/.
    Returns the résumé text, the saved file paths, and whether the source ATS
    lets you apply without an account."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    try:
        result = resume_builder_service.build_and_save(job)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Résumé build failed: {exc}")
    return result


@router.get("/{job_id}/resume")
def get_saved_resume(job_id: int):
    """Fetch the résumé previously built for this job (kept on disk for
    reference if you later get a call). Returns {exists: false} if none."""
    saved = resume_builder_service.load_saved(job_id)
    if not saved:
        return {"exists": False}
    return {"exists": True, **saved}


@router.get("/stats/summary")
def stats_summary(session: Session = Depends(get_session)):
    jobs = session.exec(select(Job)).all()
    by_status: dict = {}
    by_source: dict = {}
    by_company: dict = {}
    rejection_reasons: dict = {}
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1
        by_source[j.source] = by_source.get(j.source, 0) + 1
        by_company[j.company_name] = by_company.get(j.company_name, 0) + 1
        if j.status == "Rejected" and j.rejection_reason:
            rejection_reasons[j.rejection_reason] = rejection_reasons.get(j.rejection_reason, 0) + 1
    return {
        "total_jobs": len(jobs),
        "good_threshold": settings.min_good_score,
        "above_threshold": sum(1 for j in jobs if j.match_score >= settings.min_good_score),
        "by_status": by_status,
        "top_sources": dict(sorted(by_source.items(), key=lambda x: -x[1])[:10]),
        "top_companies": dict(sorted(by_company.items(), key=lambda x: -x[1])[:10]),
        "common_rejection_reasons": dict(sorted(rejection_reasons.items(), key=lambda x: -x[1])[:10]),
    }
