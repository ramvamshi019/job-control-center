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
        # Additive: which side of the filter this row came from. Nullable
        # for legacy rows, backfilled to 'rejected_pool' on next run.
        try:
            c.execute(text("ALTER TABLE filter_sanity_check ADD COLUMN source_pool TEXT"))
        except Exception:
            pass


def _sample(status_in: list[str], n: int) -> list[dict]:
    """Random sample of jobs discovered in the last 24h with status in
    the given list. Uses ORDER BY RANDOM() LIMIT n — bounded by 24h window
    so we're always testing the CURRENT filter behavior."""
    d24 = utcnow_naive() - timedelta(hours=24)
    placeholders = ",".join(f":s{i}" for i in range(len(status_in)))
    params = {f"s{i}": s for i, s in enumerate(status_in)}
    params.update({"d": d24, "n": n})
    with engine.connect() as c:
        c.execute(text("PRAGMA busy_timeout = 30000"))
        rows = c.execute(text(f"""
            SELECT j.id, j.title, j.company_name, j.location,
                   j.description, j.rejection_reason, j.status AS current_status,
                   COALESCE(co.h1b_history_score, 0) AS h1b_score
            FROM jobs j
            LEFT JOIN companies co ON co.id = j.company_id
            WHERE j.status IN ({placeholders}) AND j.discovered_at >= :d
            ORDER BY RANDOM()
            LIMIT :n
        """), params).all()
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


def _judge_one(client, job: dict, pool: str) -> dict | None:
    desc = (job.get("description") or "")[:1800]
    if pool == "rejected":
        framing = (
            "Ram's filter REJECTED this job. Should it have?\n"
            f"- Filter's reason for reject: {job.get('rejection_reason') or 'unknown'}\n"
        )
        bias = ("Bias: default to \"reject\" unless the job clearly fits Ram's "
                "profile (role + skills + US-based + not obviously senior/exec/leadership).")
    else:  # accepted pool
        framing = (
            f"Ram's filter ACCEPTED this job (current status: {job.get('current_status')}). Should it have?\n"
            "Red flags to watch for: senior/staff/director/manager titles, "
            "non-US location that slipped through, wrong domain (frontend/mobile), "
            "on-site only in Ram-hostile city.\n"
        )
        bias = ("Bias: default to \"keep\" unless there's a clear mismatch. "
                "Only flag as \"reject\" if you'd tell Ram this is a waste of his click.")
    prompt = f"""You're the second-opinion reviewer for Ram's job filter.
{framing}
Ram's profile:
- Skills: {settings.my_skills}
- Target roles: {settings.my_target_roles}
- Work auth: {settings.my_work_auth}
- Location: US-only, remote or on-site

Job under review:
- Title: {job['title']}
- Company: {job['company_name']} (H-1B score {job['h1b_score']}/100)
- Location: {job['location']}
- Description (truncated): {desc}

Return ONLY a JSON object, no markdown fences, no preface:
{{
  "verdict": "keep" or "reject",
  "reason": "<one sentence, 15 words max, why keep-worthy or correctly-rejected>"
}}

{bias}
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


def _store(job_id: int, verdict: dict, pool: str) -> None:
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO filter_sanity_check (job_id, verdict, reason, sampled_at, source_pool)
            VALUES (:jid, :v, :r, :ts, :p)
            ON CONFLICT(job_id) DO UPDATE SET
                verdict = excluded.verdict,
                reason = excluded.reason,
                sampled_at = excluded.sampled_at,
                source_pool = excluded.source_pool
        """), {
            "jid": job_id,
            "v": (verdict.get("verdict") or "").strip().lower(),
            "r": (verdict.get("reason") or "").strip(),
            "ts": utcnow_naive(),
            "p": pool,
        })


def run() -> dict:
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        log.info("filter_sanity: no anthropic key, skip")
        return {"sampled": 0}

    _ensure_table()

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def _run_pool(status_in: list[str], pool: str, n: int) -> dict:
        sample = _sample(status_in, n)
        keep = reject = failed = 0
        for j in sample:
            v = _judge_one(client, j, pool)
            if not v:
                failed += 1
                continue
            _store(j["id"], v, pool)
            if v.get("verdict") == "keep":
                keep += 1
            else:
                reject += 1
        return {"keep": keep, "reject": reject, "failed": failed, "n": len(sample)}

    # Rejected pool -> false-negatives (Claude says 'keep' means filter missed a good one)
    rej = _run_pool(["Rejected"], "rejected", SAMPLE_SIZE)
    # Accepted pool -> false-positives (Claude says 'reject' means filter let a bad one through)
    acc = _run_pool(["New", "Need Review"], "accepted", 15)

    false_neg = round(100 * rej["keep"] / max(rej["keep"] + rej["reject"], 1), 1)
    false_pos = round(100 * acc["reject"] / max(acc["keep"] + acc["reject"], 1), 1)
    log.info("filter_sanity: rejected pool keep=%d reject=%d fn=%.1f%% | accepted pool keep=%d reject=%d fp=%.1f%%",
             rej["keep"], rej["reject"], false_neg,
             acc["keep"], acc["reject"], false_pos)
    return {
        "rejected": rej, "accepted": acc,
        "false_neg_rate": false_neg, "false_pos_rate": false_pos,
    }


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
