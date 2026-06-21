"""
services/resume_tailor.py
-------------------------
Generates HONEST resume tailoring notes for a job. It NEVER invents experience.
It only:
  - suggests which base resume to use (Data/Cloud/Software)
  - lists which of YOUR skills (from .env MY_SKILLS) match the posting
  - flags missing keywords to consider adding ONLY if you truly have them
  - drafts a role-specific summary line you can adapt

Works fully offline (rule-based). If AI_PROVIDER + key are set, it asks the
model for a richer draft, with the same honesty guardrails, and falls back to
rule-based if the API call fails.
"""

from __future__ import annotations

from typing import List

from app.config import settings
from app.models.job import Job
from app.utils.logging import get_logger
from app.utils.text import normalize, term_in

log = get_logger("resume_tailor")


def pick_base_resume(title: str) -> str:
    t = normalize(title)
    if "data" in t or "etl" in t or "analytics" in t:
        return "base_data_engineer"
    if "cloud" in t or "devops" in t or "infrastructure" in t or "platform" in t:
        return "base_cloud_engineer"
    return "base_software_engineer"


def _matched_and_missing(desc: str) -> tuple[List[str], List[str]]:
    matched = [s for s in settings.skills_list if s and term_in(desc, s)]
    # "Missing" = common JD keywords NOT already in your skill list (suggestions only).
    common = [
        "python", "sql", "aws", "azure", "gcp", "spark", "airflow", "kafka",
        "snowflake", "docker", "kubernetes", "terraform", "etl", "ci/cd",
        "java", "scala", "react", "rest", "microservices", "linux", "git",
        "dbt", "go", "rust", "kotlin", "graphql", "flink",
    ]
    missing = [k for k in common if term_in(desc, k) and k not in settings.skills_list]
    return matched, missing


def _rule_based(job: Job) -> str:
    desc = normalize(job.description)
    base = pick_base_resume(job.title)
    matched, missing = _matched_and_missing(desc)

    lines = [
        f"### Resume tailoring notes — {job.title} @ {job.company_name}",
        "",
        f"**Suggested base resume:** `{base}.md`",
        "",
        "**Your skills that this JD asks for (lead with these):**",
        ("- " + ", ".join(matched)) if matched else "- (none auto-detected — read the JD manually)",
        "",
        "**Keywords in the JD you should add ONLY if you genuinely have them:**",
        ("- " + ", ".join(missing)) if missing else "- (no obvious gaps)",
        "",
        "**Suggested summary line (edit to keep it true):**",
        f"- Entry-level {job.title.lower()} with hands-on {(', '.join(matched[:4]) or 'data/cloud')} "
        f"experience, seeking a full-time U.S. role.",
        "",
        "**Honesty reminder:** Do not claim experience you don't have. Reorder and "
        "rephrase real bullets to surface the matching skills first.",
    ]
    return "\n".join(lines)


def _ai_based(job: Job) -> str:
    """Optional richer draft via Claude/OpenAI. Returns '' on any problem."""
    prompt = (
        "You write HONEST resume tailoring notes. Never invent experience or skills. "
        "Given the candidate's known skills and a job description, produce: (1) which "
        "base resume to use, (2) which existing skills to lead with, (3) which JD "
        "keywords to add ONLY if genuinely true, (4) a one-line summary. Keep it short.\n\n"
        f"Candidate skills: {', '.join(settings.skills_list)}\n"
        f"Work authorization: {settings.my_work_auth}\n\n"
        f"Job title: {job.title}\nCompany: {job.company_name}\n"
        f"Job description:\n{job.description[:3500]}"
    )
    try:
        if settings.ai_provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            msg = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        if settings.ai_provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            resp = client.chat.completions.create(
                model=settings.openai_model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("AI resume tailoring failed, using rule-based: %s", exc)
    return ""


def generate(job: Job) -> str:
    if settings.ai_enabled:
        ai = _ai_based(job)
        if ai:
            return ai
    return _rule_based(job)
