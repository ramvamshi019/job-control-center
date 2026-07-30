"""
scripts/interview_prep_batch.py
-------------------------------
Nightly: for each job Ram applied to in the last 7 days that doesn't
yet have a cached interview prep sheet, ask Claude to write one and
store it in ai_interview_prep table. Dashboard's 🎓 Interview Prep page
then serves the cache instantly — no waiting 15s per generation, no
duplicate Claude calls.

Prompt mirrors the dashboard's existing prep prompt so what Ram sees
matches what he was ever going to get on-demand.

Cron: 05:25 UTC (between hn_buzz 05:20 and ai_rank 05:30).
Cost: ~10 jobs/week x ~3k tokens x sonnet-5 ≈ $0.10/night. Trivial.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("interview_prep_batch")


def _ensure_table() -> None:
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_interview_prep (
                job_id       INTEGER PRIMARY KEY,
                content_md   TEXT,
                generated_at TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        """))


def _pick_candidates() -> list[dict]:
    d7 = utcnow_naive() - timedelta(days=7)
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.description
            FROM jobs j
            LEFT JOIN ai_interview_prep p ON p.job_id = j.id
            WHERE j.status = 'Applied'
              AND j.updated_at >= :d
              AND p.job_id IS NULL
            ORDER BY j.updated_at DESC
            LIMIT 20
        """), {"d": d7}).all()
        return [dict(r._mapping) for r in rows]


def _gen_one(client, job: dict) -> str | None:
    desc = (job.get("description") or "")[:3500]
    prompt = f"""You're an interview coach helping someone prep for the below role. Generate a focused prep sheet in Markdown. Be specific and grounded in the JD's actual language — no generic "tell me about yourself" filler unless clearly relevant.

# Role
Title: {job['title']}
Company: {job['company_name']}
Location: {job.get('location', 'N/A')}

# Job Description (truncated)
{desc}

# Candidate profile
F-1 OPT candidate, data engineering background, 2 yrs experience.
Skills: {settings.my_skills}
Target roles: {settings.my_target_roles}

# Output format (Markdown)

## 🧠 10 Behavioral Questions (STAR-ready)
For each: the question + a **1-line hook** on which experience to lead with (based on typical DE background — pipelines, migrations, incident response, cross-team work). No full answers, just prompts.

## 🔧 5 Technical Questions (from THIS JD's stack)
Grounded in what the JD actually mentions. Include one system-design question if the JD hints at scale. For each: the question + a 1-line hint on how to approach it.

## 🎯 3 Company-Specific Talking Points
What to research + what to bring up as "I've been following your work on X". Extract from the JD if possible, else infer from company name.

## ❓ 3 Smart Questions to Ask THEM at the end
Not generic — tied to something the JD mentions. Ex: 'You mentioned migrating to Iceberg — what's the timeline and what's the current bottleneck?'
"""
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text" or hasattr(block, "text"):
                try:
                    return block.text.strip()
                except AttributeError:
                    continue
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("job %d: Claude call failed: %s", job["id"], e)
        return None


def _store(job_id: int, content: str) -> None:
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO ai_interview_prep (job_id, content_md, generated_at)
            VALUES (:jid, :md, :ts)
            ON CONFLICT(job_id) DO UPDATE SET
                content_md = excluded.content_md,
                generated_at = excluded.generated_at
        """), {"jid": job_id, "md": content, "ts": utcnow_naive()})


def run() -> dict:
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        log.info("interview_prep_batch: no anthropic, skip")
        return {"generated": 0}
    _ensure_table()
    candidates = _pick_candidates()
    log.info("interview_prep_batch: %d jobs to prep", len(candidates))
    if not candidates:
        return {"generated": 0}

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    generated = failed = 0
    for j in candidates:
        content = _gen_one(client, j)
        if not content:
            failed += 1
            continue
        _store(j["id"], content)
        generated += 1
    log.info("interview_prep_batch: generated=%d failed=%d", generated, failed)
    return {"generated": generated, "failed": failed}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
