"""
scripts/tailor_top_n.py
-----------------------
On-demand AI resume-tailor for the day's top-N un-tailored jobs.

The scheduler ALREADY generates rule-based resume_notes for every high-score
job on ingest, and optionally calls Claude when AI is enabled. This script is
different: it's an OPT-IN batch you run manually (Ram disabled nightly Claude
crons to stop passive burn — see memory `overnight-2026-07-30`) that generates
DEEPLY tailored bullets grounded in the actual base-resume text.

The prompt is fed:
  1. The relevant BASE RESUME markdown (data / cloud / software), picked by
     the job's cluster — never invents skills not on the resume.
  2. The JD.
  3. Ram's work-auth + preferences.

Output: 3-5 tailored bullet lines the user pastes over the resume's existing
Experience bullets before applying. Overwrites job.resume_notes so the
dashboard's Resume Notes column immediately shows the tailored draft.

Selection (in order):
  1. status='New' AND match_score >= --min-score (default 60) AND resume_notes
     empty or starts with '### Resume tailoring notes' (rule-based sentinel).
  2. Optionally boost with AI fit_score if job_ai_ranking has run.
  3. LIMIT --n (default 15).

Runs to completion in one pass. Silent no-op if AI_PROVIDER != anthropic.

    # Default: top 15 New jobs with score >= 60
    ../.venv/bin/python scripts/tailor_top_n.py

    # Just print what WOULD run — no API calls, no writes
    ../.venv/bin/python scripts/tailor_top_n.py --dry-run

    # Loosen to include Need Review jobs
    ../.venv/bin/python scripts/tailor_top_n.py --include-need-review --n 25
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import Session, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.models.job import Job  # noqa: E402
from app.services.cluster import label as cluster_label  # noqa: E402
from app.services.resume_tailor import pick_base_resume  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("tailor_top_n")

# Base-resume files live at repo-root /resumes; the CLI runs from backend/.
RESUME_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "resumes")

# Rule-based fallback writes notes starting with this string — used to decide
# whether an existing resume_notes value is "already AI-tailored" (skip) or
# "just the rule-based placeholder" (overwrite).
RULE_BASED_SENTINEL = "### Resume tailoring notes"


def load_base_resume(base_slug: str) -> str:
    path = os.path.join(RESUME_DIR, f"{base_slug}.md")
    if not os.path.exists(path):
        log.warning("base resume not found: %s — falling back to base_master", path)
        path = os.path.join(RESUME_DIR, "base_master.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_prompt(job: Job, base_resume: str) -> str:
    return (
        "You are helping Ram tailor his resume for one specific job. NEVER invent "
        "experience or skills not present in the base resume — you may ONLY reorder, "
        "emphasize, or rephrase what's already there.\n\n"
        "Output EXACTLY this format:\n"
        "1. A one-line summary tailored to this JD (rewrite the base Summary, keep it true).\n"
        "2. 3-5 tailored EXPERIENCE bullets, each starting with a strong verb, each grounded "
        "in a real project from the base resume. Emphasize the specific technologies / "
        "responsibilities this JD asks for.\n"
        "3. A one-line 'why me' sentence Ram can paste into the cover-letter box.\n"
        "4. Any red flags in the JD (visa, clearance, seniority mismatch) — one line each, "
        "or 'none' if all clear.\n\n"
        "Do not add commentary, do not repeat the JD, do not use markdown headings. "
        "Number the sections 1-4 exactly.\n\n"
        f"### Work authorization\n{settings.my_work_auth}\n\n"
        f"### Base resume (ground truth — do NOT go beyond this)\n{base_resume}\n\n"
        f"### Job\nTitle: {job.title}\nCompany: {job.company_name}\n"
        f"Location: {job.location}\nCluster: {cluster_label(getattr(job, 'cluster', '') or 'other')}\n\n"
        f"### Job description\n{(job.description or '')[:5000]}"
    )


def call_claude(prompt: str) -> str:
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("Claude call failed: %s", exc)
        return ""


def eligible_jobs(min_score: int, include_need_review: bool, n: int) -> list[Job]:
    with Session(engine) as s:
        q = select(Job).where(
            Job.match_score >= min_score,
            Job.status.in_(("New", "Need Review") if include_need_review else ("New",)),
            Job.sponsorship_risk.in_(("low", "medium")),
        ).order_by(Job.match_score.desc(), Job.discovered_at.desc()).limit(n * 3)
        candidates = list(s.exec(q))

    # Post-filter: skip jobs whose notes are already AI-tailored (don't start with rule sentinel).
    fresh = [
        j for j in candidates
        if not j.resume_notes or j.resume_notes.startswith(RULE_BASED_SENTINEL)
    ]
    return fresh[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=15, help="max jobs to tailor")
    ap.add_argument("--min-score", type=int, default=60)
    ap.add_argument("--include-need-review", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and settings.ai_provider != "anthropic":
        print(f"AI provider is '{settings.ai_provider}' — set AI_PROVIDER=anthropic + "
              "ANTHROPIC_API_KEY to enable. (--dry-run works without a key.)")
        return 0

    jobs = eligible_jobs(args.min_score, args.include_need_review, args.n)
    print(f"eligible jobs: {len(jobs)} (min_score={args.min_score}, "
          f"include_need_review={args.include_need_review}, n={args.n})")
    if not jobs:
        print("nothing to do")
        return 0

    if args.dry_run:
        for j in jobs:
            base = pick_base_resume(j.title, getattr(j, "cluster", ""))
            print(f"  [{j.match_score:2d}] {j.title[:60]!r} @ {j.company_name} → base={base}")
        print(f"\n(dry run — would tailor {len(jobs)} jobs)")
        return 0

    done = 0
    with Session(engine) as s:
        for j in jobs:
            base_slug = pick_base_resume(j.title, getattr(j, "cluster", ""))
            base_resume = load_base_resume(base_slug)
            prompt = build_prompt(j, base_resume)
            print(f"[{done+1}/{len(jobs)}] {j.title[:55]!r} @ {j.company_name}  (base={base_slug})",
                  flush=True)
            tailored = call_claude(prompt)
            if not tailored:
                print(f"  -> Claude returned empty, skipping")
                continue
            # Refresh from db in case an ingest re-touched the row.
            j_db = s.get(Job, j.id)
            if j_db is None:
                continue
            j_db.resume_notes = tailored
            j_db.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            s.add(j_db)
            s.commit()
            done += 1
    print(f"\ntailored {done}/{len(jobs)} jobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
