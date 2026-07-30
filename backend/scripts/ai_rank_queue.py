"""
scripts/ai_rank_queue.py
------------------------
Overnight AI-ranking pass. Picks the top ~40 fresh (last 24h) 'New' sponsor
jobs, sends each to Claude with Ram's profile, and stores a per-job
fit_score / reasons / red_flags / pitch_line in a `job_ai_ranking` table.

Morning brief (05:30 UTC before the 06:00 UTC brief) reads this table to
surface the *AI-ranked* top of the queue instead of the heuristic top.
Ram wakes up to "here are 5 jobs Claude actually thinks fit, with a pitch
line you can paste into the cover-letter box."

Idempotent: rows are UPSERTed on job_id. Fails-open per-job (one bad
response can't nuke the batch). Cheap: ~40 jobs x sonnet-5 with 400
input tokens each = well under $1 per night.

Cron on droplet at 05:30 UTC (30 min before morning_brief.py).
Silent no-op if AI_PROVIDER != anthropic.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.utils.dates import utcnow_naive  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("ai_rank_queue")

BATCH_SIZE = int(os.environ.get("AI_RANK_BATCH", "40"))
DESC_TRUNC = 2400


def _ensure_table() -> None:
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS job_ai_ranking (
                job_id       INTEGER PRIMARY KEY,
                fit_score    INTEGER,
                reasons      TEXT,
                red_flags    TEXT,
                pitch_line   TEXT,
                raw_json     TEXT,
                generated_at TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        """))
        c.execute(text("CREATE INDEX IF NOT EXISTS idx_jar_score ON job_ai_ranking(fit_score DESC)"))


def _pick_candidates() -> list[dict]:
    """Yesterday's MISSED backlog only: jobs discovered 24-48h ago that
    Ram never actioned (still 'New' or 'Need Review'). This is the pool
    worth AI-ranking overnight — every-fresh-job would burn tokens on
    posts Ram might still catch during the day."""
    now = utcnow_naive()
    d24 = now - timedelta(hours=24)
    d48 = now - timedelta(hours=48)
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location, j.job_url,
                   j.description, j.match_score,
                   COALESCE(co.h1b_history_score, 0) AS h1b_score
            FROM jobs j
            LEFT JOIN companies co ON co.id = j.company_id
            LEFT JOIN job_ai_ranking r ON r.job_id = j.id
            WHERE j.discovered_at >= :d48 AND j.discovered_at < :d24
              AND j.status IN ('New', 'Need Review')
              AND COALESCE(co.h1b_history_score, 0) >= 40
              AND r.job_id IS NULL
            ORDER BY j.match_score DESC, j.discovered_at DESC
            LIMIT :n
        """), {"d24": d24, "d48": d48, "n": BATCH_SIZE}).all()
        return [dict(r._mapping) for r in rows]


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json(text_out: str) -> dict | None:
    # Claude usually returns bare JSON but with thinking/text-block mix
    # you sometimes get a preface. Grab the first {...} object.
    try:
        return json.loads(text_out)
    except (ValueError, TypeError):
        pass
    m = _JSON_RE.search(text_out or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _rank_one(client, job: dict) -> dict | None:
    desc = (job.get("description") or "")[:DESC_TRUNC]
    prompt = f"""You're scoring a job for Ram, an F-1 OPT data engineer.

Ram's profile:
- Skills: {settings.my_skills}
- Target roles: {settings.my_target_roles}
- Work auth: {settings.my_work_auth}
- Location: US-based, open to remote or on-site anywhere in US

Job to score:
- Title: {job['title']}
- Company: {job['company_name']} (H-1B history score: {job['h1b_score']}/100)
- Location: {job['location']}
- Description (truncated): {desc}

Return ONLY a JSON object, no preface, no markdown fences:
{{
  "fit_score": <int 0-100, honest fit for Ram given skills + role + sponsorship>,
  "reasons": ["<3-8 word reason>", "<reason>", "<reason>"],
  "red_flags": ["<red flag or empty>"],
  "pitch_line": "<one sentence Ram can paste into a cover letter, first-person, specific to this JD, 25-35 words>"
}}"""
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        # Sonnet-5 with thinking: iterate to first block with .text
        raw = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text" or hasattr(block, "text"):
                try:
                    raw = block.text.strip()
                    break
                except AttributeError:
                    continue
        parsed = _extract_json(raw)
        if not parsed or "fit_score" not in parsed:
            log.warning("job %d: no valid JSON in response (raw=%r)", job["id"], raw[:120])
            return None
        return {"parsed": parsed, "raw": raw}
    except Exception as e:  # noqa: BLE001
        log.warning("job %d: Claude call failed: %s", job["id"], e)
        return None


def _store(job_id: int, result: dict) -> None:
    p = result["parsed"]
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO job_ai_ranking
                (job_id, fit_score, reasons, red_flags, pitch_line, raw_json, generated_at)
            VALUES
                (:jid, :fs, :rs, :rf, :pl, :rj, :ts)
            ON CONFLICT(job_id) DO UPDATE SET
                fit_score = excluded.fit_score,
                reasons = excluded.reasons,
                red_flags = excluded.red_flags,
                pitch_line = excluded.pitch_line,
                raw_json = excluded.raw_json,
                generated_at = excluded.generated_at
        """), {
            "jid": job_id,
            "fs": int(p.get("fit_score", 0)),
            "rs": json.dumps(p.get("reasons") or []),
            "rf": json.dumps(p.get("red_flags") or []),
            "pl": (p.get("pitch_line") or "").strip(),
            "rj": result["raw"],
            "ts": utcnow_naive(),
        })


def run() -> dict:
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        log.info("ai_rank_queue: AI provider not anthropic, skip")
        return {"ranked": 0, "skipped": "no_ai"}

    _ensure_table()
    candidates = _pick_candidates()
    log.info("ai_rank_queue: %d candidates to rank", len(candidates))
    if not candidates:
        return {"ranked": 0}

    import anthropic  # local import so no-AI environments don't blow up
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    ranked = 0
    failed = 0
    for j in candidates:
        result = _rank_one(client, j)
        if not result:
            failed += 1
            continue
        _store(j["id"], result)
        ranked += 1
    log.info("ai_rank_queue: ranked=%d failed=%d", ranked, failed)
    return {"ranked": ranked, "failed": failed}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
