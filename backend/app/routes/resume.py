"""
routes/resume.py
----------------
Upload a résumé and get jobs ranked by fit to YOUR résumé, with experience-level
and work-authorization (citizenship/sponsorship) filters.

POST /resume/match   (multipart: file=<résumé>, plus optional form filters)
    -> { profile: {...parsed...}, count, jobs: [ {..., fit_count, fit_pct, matched_skills, experience_level} ] }
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlmodel import select

from app.database import session_scope
from app.models.job import Job
from app.services import filter_engine, resume_builder, resume_parser
from app.utils.text import normalize

router = APIRouter(prefix="/resume", tags=["resume"])


@router.get("/profile")
def profile():
    """Your standard application fields (name, email, phone, etc.) for the
    dashboard's one-click autofill/copy kit."""
    return resume_builder.get_profile()

SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|director|manager|architect)\b", re.I)
YEARS_RE = re.compile(r"(\d{1,2})\+?\s*years?", re.I)
CANDIDATE_CAP = 6000


def job_years_required(text: str) -> Optional[int]:
    nums = [int(n) for n in YEARS_RE.findall(text or "")]
    return min(nums) if nums else None


def job_experience_level(title: str, description: str) -> str:
    """Classify a job's required experience: entry / mid / senior."""
    if SENIOR_TITLE.search(title or ""):
        return "senior"
    yrs = job_years_required(description or "")
    if yrs is None:
        return "entry"          # unstated -> treat as entry-friendly
    if yrs <= 2:
        return "entry"
    if yrs <= 5:
        return "mid"
    return "senior"


def _match(data: bytes, filename: str, experience_levels: str,
           sponsor_only: bool, usa_only: bool, posted_within_hours: int,
           min_skills: int, limit: int) -> dict:
    """Heavy work (parse + scan thousands of jobs) — runs in a worker thread so
    it never blocks the API event loop. Opens its own short-lived DB session."""
    try:
        text = resume_parser.extract_text(data, filename)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read résumé: {exc}")
    profile = resume_parser.parse_resume(text)
    resume_skills = profile["skills"]
    if not resume_skills:
        return {"profile": profile, "count": 0, "jobs": [],
                "note": "No known skills detected — is this a résumé? Try a PDF/DOCX export."}

    # ONE combined word-boundary regex over all résumé skills (longest first so
    # "spark sql" wins over "spark"). findall does a single pass per description
    # instead of N separate searches — ~Nx faster across thousands of jobs.
    skill_set = set(resume_skills)
    alts = "|".join(re.escape(s) for s in sorted(resume_skills, key=len, reverse=True))
    combined = re.compile(r"(?<![a-z0-9])(" + alts + r")(?![a-z0-9])")
    wanted_levels = {x.strip().lower() for x in experience_levels.split(",") if x.strip()}

    # Select only the columns we need (tuples) — far cheaper than hydrating full
    # ORM objects with every column/description for thousands of rows.
    with session_scope() as session:
        stmt = (select(Job.id, Job.title, Job.company_name, Job.location, Job.job_url,
                       Job.source, Job.status, Job.match_score, Job.sponsorship_risk,
                       Job.posted_at, Job.description)
                .where(Job.status.in_(("New", "Need Review"))))  # type: ignore
        if posted_within_hours and posted_within_hours > 0:
            cutoff = datetime.utcnow() - timedelta(hours=posted_within_hours)
            stmt = stmt.where(Job.posted_at != None, Job.posted_at >= cutoff)  # noqa: E711
        rows = session.exec(stmt.order_by(Job.match_score.desc()).limit(CANDIDATE_CAP)).all()

    results = []
    for (jid, title, company_name, location, job_url, source, status,
         match_score, sponsorship_risk, posted_at, description) in rows:
        if sponsor_only and sponsorship_risk not in ("low", "medium"):
            continue
        # USA-only: STRICT — require a positive US signal (state/US-term/remote),
        # so foreign cities like "Gurugram" with no country marker are excluded.
        if usa_only and not filter_engine.looks_us_strict(location):
            continue
        level = job_experience_level(title, description)
        if wanted_levels and level not in wanted_levels:
            continue
        blob = normalize((title or "") + " . " + (description or ""))
        matched = sorted(set(combined.findall(blob)) & skill_set)
        if len(matched) < max(1, min_skills):
            continue
        results.append({
            "id": jid, "title": title, "company_name": company_name,
            "location": location, "job_url": job_url, "source": source,
            "status": status, "match_score": match_score,
            "sponsorship_risk": sponsorship_risk,
            "posted_at": posted_at.isoformat() if posted_at else None,
            "experience_level": level,
            "fit_count": len(matched),
            "fit_pct": round(100 * len(matched) / len(resume_skills)),
            "matched_skills": sorted(matched),
        })

    results.sort(key=lambda r: (r["fit_count"], r["match_score"]), reverse=True)
    return {"profile": profile, "count": len(results), "jobs": results[:limit]}


@router.post("/match")
async def match_resume(
    file: UploadFile = File(...),
    experience_levels: str = Form("entry,mid"),     # csv of entry/mid/senior
    sponsor_only: bool = Form(True),                # hide citizenship/clearance-required
    usa_only: bool = Form(True),                    # require a US (or remote-US) location
    posted_within_hours: int = Form(0),             # 0 = any; else 24/72/168/720
    min_skills: int = Form(1),                      # min overlapping skills
    limit: int = Form(100),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    # Offload the blocking parse+scan so concurrent requests (e.g. /health) stay snappy.
    return await run_in_threadpool(
        _match, data, file.filename or "", experience_levels, sponsor_only,
        usa_only, posted_within_hours, min_skills, limit
    )
