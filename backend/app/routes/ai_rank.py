"""
routes/ai_rank.py
-----------------
Endpoint backing the 🚀 AI-Ranked Apply Queue dashboard page.

  GET /ai_rank/queue  -> jobs joined with job_ai_ranking, ordered by AI fit_score

Reads the job_ai_ranking table that ai_rank_queue.py populates each night
at 05:30 UTC. Table is auto-created by the ranker; endpoint is defensive
(returns empty list if the table doesn't exist yet — first-morning case).
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from sqlalchemy import text

from app.database import engine

router = APIRouter(prefix="/ai_rank", tags=["ai_rank"])


@router.get("/queue")
def queue(limit: int = 40, min_fit: int = 0):
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        # Defensive: table may not exist on very first deploy (ranker hasn't run yet).
        exists = c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_ai_ranking'"
        )).scalar()
        if not exists:
            return {"count": 0, "items": [], "message": "AI ranker hasn't run yet — first pass at 05:30 UTC."}
        # Defensive: keywords column added in a later migration; SELECT via
        # PRAGMA-safe COALESCE-through-CASE isn't needed since ALTER TABLE
        # ADD COLUMN in _ensure_table backfills nulls. Just select it directly.
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                   j.match_score, j.status, j.discovered_at,
                   COALESCE(co.h1b_history_score, 0) AS h1b_score,
                   r.fit_score, r.reasons, r.red_flags, r.pitch_line,
                   r.keywords, r.referral_dm, r.salary_json,
                   r.clear_odds, r.clear_blockers, r.generated_at
            FROM job_ai_ranking r
            JOIN jobs j ON j.id = r.job_id
            LEFT JOIN companies co ON co.id = j.company_id
            WHERE r.fit_score >= :m
              AND j.status IN ('New', 'Need Review', 'Approved')
            ORDER BY r.fit_score DESC, j.discovered_at DESC
            LIMIT :n
        """), {"m": min_fit, "n": limit}).all()

    items = []
    for r in rows:
        d = dict(r._mapping)
        for f in ("reasons", "red_flags", "keywords", "clear_blockers"):
            try:
                d[f] = json.loads(d[f] or "[]")
            except (ValueError, TypeError):
                d[f] = []
        try:
            d["salary"] = json.loads(d.pop("salary_json", None) or "{}")
        except (ValueError, TypeError):
            d["salary"] = {}
        items.append(d)
    return {"count": len(items), "items": items}


@router.post("/run_ranker")
def run_ranker(batch: int = 40):
    """Kick the AI ranker manually — on-demand button on the dashboard.
    Nightly cron is disabled (2026-07-30) to stop passive Claude burn.
    Uses a small batch by default so a click can't accidentally cost \$$$.
    """
    import os, subprocess
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() in ("", None):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set on backend"}
    # Env override — user picks 20/40/100 in the UI. Cap at 200.
    batch = max(5, min(int(batch or 40), 200))
    env = os.environ.copy()
    env["AI_RANK_BATCH"] = str(batch)
    # Fire and don't wait — response returns immediately. User can watch
    # progress via /ai_rank/overnight_status which reads the artifact table.
    subprocess.Popen(
        ["python", "scripts/ai_rank_queue.py"],
        cwd="/app/backend", env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "batch": batch, "message": f"ranker started on up to {batch} jobs"}


@router.post("/run_filter_sanity")
def run_filter_sanity():
    """On-demand filter sanity check — was nightly, now click-to-run."""
    import os, subprocess
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() in ("", None):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set on backend"}
    subprocess.Popen(
        ["python", "scripts/filter_sanity_check.py"],
        cwd="/app/backend",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "message": "sanity check started (~35 samples, ~$0.15)"}


@router.get("/overnight_status")
def overnight_status():
    """Report last-run status of each overnight cron by inspecting the
    artifact tables. Dashboard's Ops Health page consumes this so Ram
    can eyeball whether the nightly jobs are firing on schedule.
    Windows: 26h so a slightly-late run still shows green."""
    from datetime import timedelta as _td
    with engine.connect() as c:
        exists = lambda t: bool(c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:t"
        ), {"t": t}).scalar())
        def _last(table: str, col: str) -> tuple[int, str | None]:
            if not exists(table):
                return 0, None
            row = c.execute(text(
                f"SELECT COUNT(*), MAX({col}) FROM {table} WHERE {col} >= :d"
            ), {"d": None}).fetchone()
            # count-over-lifetime for status; last-run timestamp separate
            total = c.execute(text(f"SELECT MAX({col}) FROM {table}")).scalar()
            last24 = c.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE {col} >= datetime('now', '-26 hours')"
            )).scalar() or 0
            return last24, str(total) if total else None
        jobs = {}
        n, ts = _last("job_ai_ranking", "generated_at")
        jobs["ai_rank_queue"] = {"last_run": ts, "rows_last_24h": n, "cron": "05:30 UTC"}
        n, ts = _last("hn_buzz", "fetched_at")
        jobs["hn_buzz"] = {"last_run": ts, "rows_last_24h": n, "cron": "05:20 UTC"}
        n, ts = _last("filter_sanity_check", "sampled_at")
        jobs["filter_sanity_check"] = {"last_run": ts, "rows_last_24h": n, "cron": "05:15 UTC"}
        n, ts = _last("ai_interview_prep", "generated_at")
        jobs["interview_prep_batch"] = {"last_run": ts, "rows_last_24h": n, "cron": "05:25 UTC"}
    return {"jobs": jobs}


@router.get("/interview_prep/{job_id}")
def interview_prep(job_id: int):
    """Return cached interview prep sheet for a job (if any).
    Populated nightly by scripts/interview_prep_batch.py for Applied jobs.
    """
    with engine.connect() as c:
        exists = c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_interview_prep'"
        )).scalar()
        if not exists:
            return {"cached": False, "content_md": None}
        row = c.execute(text(
            "SELECT content_md, generated_at FROM ai_interview_prep WHERE job_id = :j"
        ), {"j": job_id}).fetchone()
    if not row:
        return {"cached": False, "content_md": None}
    return {"cached": True, "content_md": row[0], "generated_at": str(row[1])}
