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
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                   j.match_score, j.status, j.discovered_at,
                   COALESCE(co.h1b_history_score, 0) AS h1b_score,
                   r.fit_score, r.reasons, r.red_flags, r.pitch_line,
                   r.generated_at
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
        for f in ("reasons", "red_flags"):
            try:
                d[f] = json.loads(d[f] or "[]")
            except (ValueError, TypeError):
                d[f] = []
        items.append(d)
    return {"count": len(items), "items": items}
