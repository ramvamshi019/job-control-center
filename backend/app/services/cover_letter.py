"""
services/cover_letter.py
------------------------
Generates a SHORT (120-180 word) cover letter draft. Professional, specific,
no fake claims. Mentions OPT only if `include_opt=True`.

Offline rule-based by default; uses Claude/OpenAI if configured, with fallback.
"""

from __future__ import annotations

from app.config import settings
from app.models.job import Job
from app.services.resume_tailor import pick_base_resume
from app.utils.logging import get_logger
from app.utils.text import normalize

log = get_logger("cover_letter")


def _top_skills(job: Job, n: int = 4) -> str:
    desc = normalize(job.description)
    matched = [s for s in settings.skills_list if s and s in desc]
    chosen = matched[:n] if matched else settings.skills_list[:n]
    return ", ".join(chosen)


def _rule_based(job: Job, include_opt: bool) -> str:
    skills = _top_skills(job)
    base = pick_base_resume(job.title).replace("base_", "").replace("_", " ")
    opt_line = (
        " I am authorized to work in the U.S. on F-1 OPT (STEM) and am excited to "
        "contribute long-term."
        if include_opt
        else ""
    )
    return (
        f"Dear {job.company_name} Hiring Team,\n\n"
        f"I'm excited to apply for the {job.title} role. My background as a "
        f"{base} has given me hands-on experience with {skills}, which map "
        f"directly to what this position calls for. In recent projects I built and "
        f"shipped reliable data/software solutions end to end, and I'm eager to bring "
        f"that same ownership to {job.company_name}.\n\n"
        f"What draws me to {job.company_name} is the chance to grow as an early-career "
        f"engineer on a team that values quality and impact. I learn fast, communicate "
        f"clearly, and care about building things that last.{opt_line}\n\n"
        f"Thank you for your time and consideration. I'd welcome the opportunity to "
        f"discuss how I can contribute.\n\n"
        f"Sincerely,\nRam Vamshi Krishna"
    )


def _ai_based(job: Job, include_opt: bool) -> str:
    opt = "Mention F-1 OPT availability briefly." if include_opt else "Do NOT mention visa status."
    prompt = (
        "Write a professional cover letter of 120-180 words. Be specific to the role "
        "and company. No fake claims, no clichés. Focus on real skills and projects. "
        f"{opt}\n\n"
        f"Candidate skills: {', '.join(settings.skills_list)}\n"
        f"Role: {job.title}\nCompany: {job.company_name}\n"
        f"Job description:\n{job.description[:3000]}"
    )
    try:
        if settings.ai_provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            msg = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return msg.content[0].text.strip()
        if settings.ai_provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            resp = client.chat.completions.create(
                model=settings.openai_model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("AI cover letter failed, using rule-based: %s", exc)
    return ""


def generate(job: Job, include_opt: bool = False) -> str:
    if settings.ai_enabled:
        ai = _ai_based(job, include_opt)
        if ai:
            return ai
    return _rule_based(job, include_opt)
