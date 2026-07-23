"""
routes/jobs.py
--------------
Read jobs (with filters) and update a job's status / reason / materials.
The dashboard talks to these endpoints.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, or_, select

from app.config import settings
from app.database import get_session
from app.models.company import Company
from app.models.job import JOB_STATUSES, Job
from app.services import cover_letter as cover_letter_service
from app.services import jobright_radar
from app.services import resume_builder as resume_builder_service
from app.services import resume_tailor as resume_service

router = APIRouter(prefix="/jobs", tags=["jobs"])

_YEARS_RE = re.compile(r"(\d{1,2})\+?\s*years?")


def _years_required(title: str | None, description: str | None) -> Optional[int]:
    """Smallest 'N years' figure in the title/description, or None. Mirrors the
    dashboard's own parser so `slim` responses band identically to full ones."""
    nums = [int(n) for n in _YEARS_RE.findall(f"{title or ''} {description or ''}".lower())]
    return min(nums) if nums else None


class JobStatusUpdate(BaseModel):
    status: Optional[str] = None
    rejection_reason: Optional[str] = None
    resume_notes: Optional[str] = None
    cover_letter: Optional[str] = None


@router.get("/")
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
    jobright_tier: Optional[str] = Query(
        None, description="Filter by JobRight-coverage tier: exclusive | likely | common"),
    order_by: str = Query("score", description="score | posted | discovered | exclusivity"),
    limit: int = Query(200, le=3000),
    slim: bool = Query(
        False,
        description="Drop the heavy `description` field and return a precomputed "
                    "`years_required` instead. For list/feed views, which never render "
                    "the description and only parse years out of it."),
):
    stmt = select(Job).where(Job.match_score >= min_score)
    if status:
        stmt = stmt.where(Job.status == status)
    # Pre-narrow by source when a JobRight tier is requested (the dominant
    # signal); the exact tier is confirmed per-row after enrichment below.
    if jobright_tier:
        _tier_sources = jobright_radar.sources_for_tier(jobright_tier)
        if _tier_sources:
            stmt = stmt.where(Job.source.in_(_tier_sources))
    if exclude_rejected:
        # Also hides "Archived" — jobs the user manually dismissed — so a dismissed
        # job disappears from every active feed, not just the one it was dismissed on.
        stmt = stmt.where(Job.status.not_in(["Rejected", "Archived"]))
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
    jobs = session.exec(stmt).all()

    # Enrich each job with its company's H-1B sponsor score so the dashboard can
    # HIGHLIGHT confirmed sponsors (score >= 50). One extra query, batched.
    cids = {j.company_id for j in jobs if j.company_id}
    scores: dict[int, int] = {}
    counts: dict[int, int] = {}
    created: dict[int, datetime] = {}
    if cids:
        for cid, sc, cr in session.exec(
            select(Company.id, Company.h1b_history_score, Company.created_at)
            .where(Company.id.in_(cids))
        ).all():
            scores[cid] = sc or 0
            if cr is not None:
                created[cid] = cr
        # Total postings per company = "footprint" for the JobRight estimate.
        from sqlalchemy import func
        for cid, n in session.exec(
            select(Job.company_id, func.count(Job.id))
            .where(Job.company_id.in_(cids))
            .group_by(Job.company_id)
        ).all():
            counts[cid] = n
    now = datetime.utcnow()
    out = []
    for j in jobs:
        d = j.model_dump()
        if slim:
            # `description` is ~62% of a feed payload (5.9 MB of 9.3 MB at
            # limit=3000) and the list views only ever parse a years-of-experience
            # figure out of it. Send that figure instead of the text.
            d["years_required"] = _years_required(j.title, j.description)
            d.pop("description", None)
        d["sponsor_score"] = scores.get(j.company_id, 0)
        d["sponsor_confirmed"] = d["sponsor_score"] >= 50
        # "board_known" = this company's board was already being crawled well
        # before this job appeared (>2 days). It lets the dashboard treat a
        # freshly-seen posting on an established board as "likely posted today"
        # even when the source hides the real date -- WITHOUT mislabelling the
        # backfill dump from a board we only just started crawling.
        _cr = created.get(j.company_id)
        d["board_known"] = bool(
            _cr is not None and j.discovered_at is not None
            and _cr < j.discovered_at - timedelta(days=2)
        )
        d.update(jobright_radar.classify(
            source=j.source,
            company_name=j.company_name,
            company_job_count=counts.get(j.company_id, 0),
            sponsor_score=d["sponsor_score"],
            discovered_at=j.discovered_at,
            now=now,
        ))
        out.append(d)

    # Confirm the exact tier per-row (source pre-filter is only an approximation)
    # and surface the strongest edges first.
    if jobright_tier:
        out = [d for d in out if d["jobright_tier"] == jobright_tier]
    if order_by == "exclusivity" or jobright_tier:
        out.sort(key=lambda d: (d["jobright_exclusivity"], d["match_score"]), reverse=True)
    return out


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


@router.get("/{job_id}/match-report")
def job_match_report(job_id: int, session: Session = Depends(get_session)):
    """Jobscan-style skill-coverage report of this job vs your master résumé.
    Fast (no AI): returns {score, matched, missing, jd_skills, counts, title_match}.
    Use it to triage which jobs are worth building a tailored résumé for."""
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return resume_builder_service.match_report_for(job)


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
    # Aggregate in SQL — never materialize every Job row (253k rows with big
    # description text would exhaust memory / time out on a small host).
    total = session.exec(select(func.count()).select_from(Job)).one()
    above = session.exec(
        select(func.count()).select_from(Job).where(Job.match_score >= settings.min_good_score)
    ).one()
    by_status = dict(session.exec(select(Job.status, func.count()).group_by(Job.status)).all())
    by_source = dict(session.exec(
        select(Job.source, func.count()).group_by(Job.source)
        .order_by(func.count().desc()).limit(10)
    ).all())
    by_company = dict(session.exec(
        select(Job.company_name, func.count()).group_by(Job.company_name)
        .order_by(func.count().desc()).limit(10)
    ).all())
    rejection_reasons = dict(session.exec(
        select(Job.rejection_reason, func.count())
        .where(Job.status == "Rejected", Job.rejection_reason.is_not(None))
        .group_by(Job.rejection_reason).order_by(func.count().desc()).limit(10)
    ).all())
    return {
        "total_jobs": total,
        "good_threshold": settings.min_good_score,
        "above_threshold": above,
        "by_status": by_status,
        "top_sources": by_source,
        "top_companies": by_company,
        "common_rejection_reasons": rejection_reasons,
    }
