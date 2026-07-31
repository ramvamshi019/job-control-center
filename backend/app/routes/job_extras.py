"""
routes/job_extras.py
--------------------
Fetch a batch of job_extras rows so the dashboard can join them client-
side. Cheaper than modifying /jobs/ to join every time.

  GET /job_extras/batch?ids=1,2,3   → {id: extras_dict}
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from sqlalchemy import text

from app.database import engine

router = APIRouter(prefix="/job_extras", tags=["job_extras"])


@router.get("/batch")
def batch(ids: Optional[str] = None, limit: int = 500):
    """Return {job_id: {years_min, years_max, citizenship, clearance,
    no_sponsor, remote_ok, salary_min, salary_max}} for the given ids
    or the most-recently-enriched N. IDs are comma-separated integers."""
    with engine.connect() as c:
        exists = c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='job_extras'"
        )).scalar()
        if not exists:
            return {"items": {}}
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:1000]
            if not id_list:
                return {"items": {}}
            placeholders = ",".join(str(i) for i in id_list)
            sql = f"SELECT * FROM job_extras WHERE job_id IN ({placeholders})"
            params = {}
        else:
            sql = "SELECT * FROM job_extras ORDER BY enriched_at DESC LIMIT :n"
            params = {"n": max(1, min(int(limit), 1000))}
        rows = c.execute(text(sql), params).all()
    out = {}
    for r in rows:
        d = dict(r._mapping)
        jid = d.pop("job_id")
        out[jid] = d
    return {"items": out}


@router.post("/run_enrich")
def run_enrich():
    """Kick the enricher manually. Pure regex, no Claude tokens."""
    import subprocess
    subprocess.Popen(
        ["python", "scripts/enrich_job_extras.py"],
        cwd="/app/backend",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "message": "enricher started (regex-only, no Claude tokens)"}
