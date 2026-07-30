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
        # Additive migration: keywords column (nullable, no default) — safe on
        # existing rows, they just show as null until re-ranked.
        try:
            c.execute(text("ALTER TABLE job_ai_ranking ADD COLUMN keywords TEXT"))
        except Exception:
            pass
        try:
            c.execute(text("ALTER TABLE job_ai_ranking ADD COLUMN referral_dm TEXT"))
        except Exception:
            pass
        try:
            c.execute(text("ALTER TABLE job_ai_ranking ADD COLUMN salary_json TEXT"))
        except Exception:
            pass
        # Reality-check pass (task 71) — separate score for "will Ram clear
        # this employer's actual filter", vs fit_score which is skill overlap.
        try:
            c.execute(text("ALTER TABLE job_ai_ranking ADD COLUMN clear_odds INTEGER"))
        except Exception:
            pass
        try:
            c.execute(text("ALTER TABLE job_ai_ranking ADD COLUMN clear_blockers TEXT"))
        except Exception:
            pass


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
  "pitch_line": "<one sentence Ram can paste into a cover letter, first-person, specific to this JD, 25-35 words>",
  "keywords": ["<top 5 skills/tools/keywords the JD emphasizes, e.g. 'Airflow', 'dbt', 'AWS Glue'>"],
  "referral_dm": "<3-sentence LinkedIn DM Ram can paste to a hiring manager or recruiter at {job['company_name']}. First sentence: warm opener ('Saw the {job['title']} opening'). Second: 1-2 concrete fit points from Ram's stack. Third: ask (would love a 15-min chat / could you refer me / any advice on landing this). No emojis. No 'Dear'. 45-60 words total.>",
  "salary": {{"min": <int USD annual or null>, "max": <int USD annual or null>, "note": "<'base', 'total comp', 'unstated', or 'hourly $X' if hourly>"}}
}}"""
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            # Was 800 pre-DM; +referral_dm needs more headroom. 1200 gives
            # enough slack for Sonnet's thinking preamble + long titles.
            max_tokens=1200,
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
        if parsed and "fit_score" in parsed:
            return {"parsed": parsed, "raw": raw}
        # Retry once with more headroom + explicit "shorter JSON" ask —
        # the failures are almost always mid-JSON truncation caused by
        # Sonnet's extended-thinking preamble eating output tokens.
        log.info("job %d: retrying with 1800 max_tokens", job["id"])
        try:
            resp = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=1800,
                messages=[{"role": "user", "content": prompt +
                    "\n\nIMPORTANT: keep reasons under 6 words each and pitch_line under 30 words. Output JSON must be complete."}],
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
            if parsed and "fit_score" in parsed:
                return {"parsed": parsed, "raw": raw}
        except Exception as e2:  # noqa: BLE001
            log.warning("job %d: retry also failed: %s", job["id"], e2)
        log.warning("job %d: no valid JSON after retry (raw=%r)", job["id"], raw[:120])
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("job %d: Claude call failed: %s", job["id"], e)
        return None


def _reality_check(client, job: dict) -> dict | None:
    """Second Claude call — 'ignore skill overlap, will Ram clear their
    actual filter?'. Catches structural gates the fit-score alone misses:
      - OSS PRs required (Hugging Face, HashiCorp, etc)
      - Senior/staff/principal-in-disguise ("Data Engineer II" == 3-5y)
      - Clearance / citizenship walls
      - In-person only in a Ram-hostile city
      - Domain-specialty specifics (iOS Data Engineer = iOS eng, not DE)
      - Contract / staffing agency reposts of the same JD
    """
    desc = (job.get("description") or "")[:DESC_TRUNC]
    prompt = f"""You're a hiring-filter reality-checker for Ram, an F-1 OPT data engineer.

Ram's profile:
- 2 years experience (J&J internship + coursework)
- Skills: {settings.my_skills}
- Target roles: {settings.my_target_roles}
- Work auth: {settings.my_work_auth}
- Public: 3 GitHub projects (JobJarvis, Ecommerce-ML, this JCC crawler). NO merged PRs to major OSS ML libraries.
- Location: US-based

Job to reality-check:
- Title: {job['title']}
- Company: {job['company_name']} (H-1B history score: {job['h1b_score']}/100)
- Location: {job['location']}
- Description (truncated): {desc}

Ignore skill overlap. Judge ONLY: will Ram clear this employer's ACTUAL filter? Look for structural blockers:
- Does the JD explicitly require OSS contributions / GitHub track record he doesn't have?
- Does the title imply seniority (II/III/Senior/Staff/Lead/Principal/Manager) beyond 2y?
- Clearance / citizenship / green-card wall?
- In-person only in NYC/SF/Bay Area/Seattle with high cost-of-living?
- Domain specialty that requires prior work in that domain (iOS, embedded, network, game, security)?
- Contract / C2C / 1099 / staffing agency repost?
- Explicit no-sponsor / no-visa language?

Return ONLY a JSON object, no preface, no markdown:
{{
  "clear_odds": <int 0-100, honest odds he clears the filter — 90+ = easy pass, 40-70 = stretch, <30 = won't clear>,
  "blockers": ["<short blocker phrase>", "<blocker>"]
}}

Bias: default to HIGH clear_odds unless you find a specific gate. Empty blockers list = full clear."""

    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=500,
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
        if not parsed or "clear_odds" not in parsed:
            log.info("job %d: reality-check missing clear_odds (raw=%r)", job["id"], raw[:120])
            return None
        return parsed
    except Exception as e:  # noqa: BLE001
        log.warning("job %d: reality-check call failed: %s", job["id"], e)
        return None


def _store(job_id: int, result: dict, reality: dict | None = None) -> None:
    p = result["parsed"]
    clear_odds = None
    blockers_json = None
    if reality:
        try:
            clear_odds = int(reality.get("clear_odds") or 0)
        except (ValueError, TypeError):
            clear_odds = 0
        blockers_json = json.dumps(reality.get("blockers") or [])
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO job_ai_ranking
                (job_id, fit_score, reasons, red_flags, pitch_line, keywords,
                 referral_dm, salary_json, clear_odds, clear_blockers,
                 raw_json, generated_at)
            VALUES
                (:jid, :fs, :rs, :rf, :pl, :kw, :dm, :sal, :co, :cb, :rj, :ts)
            ON CONFLICT(job_id) DO UPDATE SET
                fit_score = excluded.fit_score,
                reasons = excluded.reasons,
                red_flags = excluded.red_flags,
                pitch_line = excluded.pitch_line,
                keywords = excluded.keywords,
                referral_dm = excluded.referral_dm,
                salary_json = excluded.salary_json,
                clear_odds = excluded.clear_odds,
                clear_blockers = excluded.clear_blockers,
                raw_json = excluded.raw_json,
                generated_at = excluded.generated_at
        """), {
            "jid": job_id,
            "fs": int(p.get("fit_score", 0)),
            "rs": json.dumps(p.get("reasons") or []),
            "rf": json.dumps(p.get("red_flags") or []),
            "pl": (p.get("pitch_line") or "").strip(),
            "kw": json.dumps(p.get("keywords") or []),
            "dm": (p.get("referral_dm") or "").strip(),
            "sal": json.dumps(p.get("salary") or {}),
            "co": clear_odds,
            "cb": blockers_json,
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
    reality_ran = 0
    for j in candidates:
        result = _rank_one(client, j)
        if not result:
            failed += 1
            continue
        # Reality-check runs only on the promising ones (fit >= 60) — no
        # point spending tokens on jobs we're already going to skip.
        reality = None
        try:
            if int(result["parsed"].get("fit_score", 0)) >= 60:
                reality = _reality_check(client, j)
                if reality:
                    reality_ran += 1
        except Exception as e:  # noqa: BLE001
            log.warning("job %d: reality wrapper error: %s", j["id"], e)
        _store(j["id"], result, reality)
        ranked += 1
    log.info("ai_rank_queue: reality checks ran on %d/%d jobs", reality_ran, ranked)

    # Auto-Approve pass: fit_score >= 85 AND clear_odds >= 70 AND sponsor.
    # BOTH scores must be high — old logic only checked skill overlap and
    # would auto-approve Hugging Face at 88 fit even though clear_odds is
    # in the 20s (needs merged Transformers PRs Ram doesn't have). Now
    # jobs that look like a skill match but won't clear the filter STAY
    # in New (Ram can review manually), not Approved.
    autoapp = 0
    try:
        with engine.begin() as c:
            res = c.execute(text("""
                UPDATE jobs SET status = 'Approved', updated_at = :ts
                WHERE id IN (
                    SELECT j.id FROM jobs j
                    JOIN job_ai_ranking r ON r.job_id = j.id
                    LEFT JOIN companies co ON co.id = j.company_id
                    WHERE j.status = 'New'
                      AND r.fit_score >= 85
                      AND (r.clear_odds IS NULL OR r.clear_odds >= 70)
                      AND COALESCE(co.h1b_history_score, 0) >= 60
                )
            """), {"ts": utcnow_naive()})
            autoapp = res.rowcount or 0
    except Exception as e:  # noqa: BLE001
        log.warning("auto-approve failed: %s", e)
    log.info("ai_rank_queue: ranked=%d failed=%d auto_approved=%d",
             ranked, failed, autoapp)
    return {"ranked": ranked, "failed": failed, "auto_approved": autoapp}


if __name__ == "__main__":
    raise SystemExit(0 if run() is not None else 1)
