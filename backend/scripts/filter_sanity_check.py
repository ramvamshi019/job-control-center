"""
scripts/filter_sanity_check.py
------------------------------
Ram's filter has rejected 753k jobs. If even 3% are wrongly filtered
(false-negatives), that's ~22,500 good jobs he never saw. Nightly this
script:

  1. Samples 20 random status='Rejected' jobs from the last 24h
  2. Asks Claude "would Ram want to see this? honest yes/no + reason"
     against his profile (skills, roles, work-auth, US-only)
  3. Stores results in filter_sanity_check table
  4. Morning brief surfaces the false-negatives + a rate ("X of 20
     samples were wrongly rejected")

If the rate creeps over 10% consistently, Ram's filters need loosening.
Cron: 05:15 UTC (before ai_rank at 05:30, so morning brief has both).
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

log = get_logger("filter_sanity")
SAMPLE_SIZE = int(os.environ.get("FILTER_SANITY_SAMPLE", "20"))


def _ensure_table() -> None:
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS filter_sanity_check (
                job_id       INTEGER PRIMARY KEY,
                verdict      TEXT,        -- 'keep' or 'reject'
                reason       TEXT,
                sampled_at   TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )
        """))


def _sample_rejected() -> list[dict]:
    d24 = utcnow_naive() - timedelta(hours=24)
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        # random() cheap here — LIMIT keeps SQLite from sorting all 753k rows,
        # but let's cap the candidate pool to last 24h so we're testing the
        # CURRENT filter behavior, not a stale one.
        rows = c.execute(text("""
            SELECT j.id, j.title, j.company_name, j.location,
                   j.description, j.rejection_reason,
                   COALESCE(co.h1b_history_score, 0) AS h1b_score
            FROM jobs j
            LEFT JOIN companies co ON co.id = j.company_id
            WHERE j.status = 'Rejected' AND j.discovered_at >= :d
            ORDER BY RANDOM()
            LIMIT :n
        """), {"d": d24, "n": SAMPLE_SIZE}).all()
        return [dict(r._mapping) for r in rows]


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json(s: str) -> dict | None:
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    m = _JSON_RE.search(s or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _judge_one(client, job: dict) -> dict | None:
    desc = (job.get("description") or "")[:1800]
    prompt = f"""You're the second-opinion reviewer for Ram's job filter.
Ram's filter REJECTED this job. Should it have?

Ram's profile:
- Skills: {settings.my_skills}
- Target roles: {settings.my_target_roles}
- Work auth: {settings.my_work_auth}
- Location: US-only, remote or on-site

Job that got rejected:
- Title: {job['title']}
- Company: {job['company_name']} (H-1B score {job['h1b_score']}/100)
- Location: {job['location']}
- Filter's reason for reject: {job.get('rejection_reason') or 'unknown'}
- Description (truncated): {desc}

Return ONLY a JSON object, no markdown fences, no preface:
{{
  "verdict": "keep" or "reject",
  "reason": "<one sentence, 15 words max, why keep-worthy or correctly-rejected>"
}}

Bias: default to "reject" unless the job clearly fits Ram's profile
(role + skills + US-based + not obviously senior/exec/leadership).
"""
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text" or hasattr(block, "text"):
                try:
                    raw = block.text.strip()
                    break
                except AttributeError:
                    continue
        parsed = _extract_json(raw)
        if not parsed or "verdict" not in parsed:
            return None
        return parsed
    except Exception as e:  # noqa: BLE001
        log.warning("job %d: Claude call failed: %s", job["id"], e)
        return None


def _store(job_id: int, verdict: dict) -> None:
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO filter_sanity_check (job_id, verdict, reason, sampled_at)
            VALUES (:jid, :v, :r, :ts)
            ON CONFLICT(job_id) DO UPDATE SET
                verdict = excluded.verdict,
                reason = excluded.reason,
                sampled_at = excluded.sampled_at
        """), {
            "jid": job_id,
            "v": (verdict.get("verdict") or "").strip().lower(),
            "r": (verdict.get("reason") or "").strip(),
            "ts": utcnow_naive(),
        })


def run() -> dict:
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        log.info("filter_sanity: no anthropic key, skip")
        return {"sampled": 0}

    _ensure_table()
    sample = _sample_rejected()
    log.info("filter_sanity: sampling %d rejected jobs", len(sample))
    if not sample:
        return {"sampled": 0}

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    keep = reject = failed = 0
    for j in sample:
        v = _judge_one(client, j)
        if not v:
            failed += 1
            continue
        _store(j["id"], v)
        if v.get("verdict") == "keep":
            keep += 1
        else:
            reject += 1
    total = keep + reject
    rate = round(100 * keep / total, 1) if total else 0
    log.info("filter_sanity: keep=%d reject=%d failed=%d false_neg_rate=%.1f%%",
             keep, reject, failed, rate)
    return {"keep": keep, "reject": reject, "failed": failed, "false_neg_rate": rate}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
