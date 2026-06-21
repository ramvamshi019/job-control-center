"""
services/resume_builder.py
--------------------------
Builds a FULL, ATS-friendly tailored résumé for a single job, on demand.

HONESTY IS THE WHOLE POINT. The builder starts from one of YOUR real base
résumés (resumes/base_*.md) and only:
  - reorders sections/bullets so the JD-relevant experience leads
  - rephrases existing bullets using the posting's terminology ONLY where that
    wording still accurately describes work you actually did
  - drops bullets irrelevant to this role
  - writes a 2-3 line summary assembled from experience already in the résumé
It NEVER invents employers, dates, titles, metrics, skills, or achievements.
Contact info, company names, dates, and education are copied verbatim.

If no Anthropic key is configured (or the call fails), it falls back to the
base résumé unchanged — which is already 100% true. So the worst case is
"honest but un-tailored", never "tailored but fabricated".

The result is written to resumes/generated/<id>_<company>.md (human-readable)
and a stripped .txt (single-column plain text — the format ATS parsers read
most reliably). Returns those paths plus the résumé text.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.models.job import Job
from app.services import latex_resume
from app.services.resume_tailor import pick_base_resume
from app.utils.logging import get_logger

log = get_logger("resume_builder")

# resumes/ and resumes/generated/ live at the project root (this file is
# backend/app/services/resume_builder.py → parents[3] is the repo root).
RESUMES_DIR = Path(__file__).resolve().parents[3] / "resumes"
GENERATED_DIR = RESUMES_DIR / "generated"

# Sources you can apply to WITHOUT creating an account — worth a tailored résumé
# because applying is one click. workday + icims force account creation, so they
# are deliberately excluded (the builder still works on them, just flagged).
LOGIN_FREE_SOURCES = {
    "greenhouse", "lever", "ashby", "bamboohr", "smartrecruiters",
    "recruitee", "workable",
}


def is_login_free(source: str) -> bool:
    return (source or "").lower() in LOGIN_FREE_SOURCES


# Your real résumé, dropped in as resumes/base_master.md, is the source of truth
# for ALL tailoring when present — it overrides the synthetic role-based bases.
MASTER_BASE = "base_master"


def resolve_base(title: str = "") -> str:
    """Use the master (your real) résumé if it exists, else fall back to the
    role-based base picked from the job title."""
    if (RESUMES_DIR / f"{MASTER_BASE}.md").exists():
        return MASTER_BASE
    return pick_base_resume(title)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "company"


def _load_base(base: str) -> str:
    """Read a base résumé and strip its internal editor scaffolding (the
    "(Base Resume)" title suffix and the "> Primary resume… never invent"
    blockquote note) so neither the fallback file nor the AI prompt carries
    meta-text into a résumé you'd actually submit."""
    path = RESUMES_DIR / f"{base}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read base résumé %s: %s", path, exc)
        return ""
    lines = []
    for line in raw.splitlines():
        if line.lstrip().startswith(">"):          # drop the editor note
            continue
        line = re.sub(r"\s*\(Base Resume\)", "", line)  # clean the H1 title
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _md_to_text(md: str) -> str:
    """Strip Markdown to single-column plain text — the most ATS-parser-safe
    form. No tables, no decoration, standard bullets."""
    out = []
    for line in md.splitlines():
        s = line.rstrip()
        s = re.sub(r"^\s*#{1,6}\s*", "", s)          # headings → plain line
        s = re.sub(r"^\s*[-*+]\s+", "- ", s)          # normalize bullets
        s = re.sub(r"^\s*>\s?", "", s)                # blockquotes
        s = s.replace("**", "").replace("__", "")     # bold
        s = re.sub(r"`([^`]*)`", r"\1", s)            # inline code
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", s)  # links → text (url)
        out.append(s)
    return "\n".join(out).strip() + "\n"


def _add_runs(paragraph, text: str) -> None:
    """Render inline **bold** within a docx paragraph; rest as normal runs."""
    for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not chunk:
            continue
        run = paragraph.add_run(chunk)
        if i % 2 == 1:  # odd chunks were inside ** **
            run.bold = True


def _to_docx(resume_md: str, path: Path) -> bool:
    """Write the résumé as a clean, single-column, ATS-friendly .docx — the
    format most application forms parse best. Returns False (and writes nothing)
    if python-docx isn't available, so callers degrade gracefully."""
    try:
        from docx import Document
        from docx.shared import Pt
    except Exception as exc:  # noqa: BLE001
        log.warning("python-docx unavailable, skipping .docx: %s", exc)
        return False

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    for raw in resume_md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            level = min(len(h.group(1)), 3)
            doc.add_heading(re.sub(r"\*\*", "", h.group(2)), level=level)
            continue
        b = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if b:
            _add_runs(doc.add_paragraph(style="List Bullet"), b.group(1))
            continue
        _add_runs(doc.add_paragraph(), re.sub(r"^\s*>\s?", "", line))
    doc.save(str(path))
    return True


def _ai_build(job: Job, base_text: str) -> str:
    """Tailor the base résumé to the job via Claude. Returns '' on any problem
    so callers fall back to the (still-true) base résumé."""
    prompt = (
        "You are an expert résumé editor. Tailor the candidate's BASE RÉSUMÉ to the "
        "job below. This is a strict editing task, not a writing task.\n\n"
        "HARD RULES — breaking any of these makes the output unusable:\n"
        "1. Use ONLY facts present in the base résumé. Never invent or add employers, "
        "job titles, dates, companies, degrees, metrics, certifications, or skills the "
        "candidate doesn't already list.\n"
        "2. Copy contact info, employer names, employment dates, and education EXACTLY "
        "as written.\n"
        "3. You MAY: reorder sections and bullets so the most relevant experience leads; "
        "drop bullets irrelevant to this role; rephrase a real bullet using the job's "
        "terminology ONLY when that wording still truthfully describes the same work; "
        "write a 2-3 line professional summary assembled from experience already present.\n"
        "4. Keep the work-authorization line truthful and unchanged in meaning.\n"
        "5. Output ATS-friendly Markdown: single column, standard section headers "
        "(Summary, Core Skills, Experience, Education), '-' bullets, no tables, no "
        "columns, no images, no emojis. Output ONLY the résumé — no preamble or notes.\n\n"
        f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company_name}\n"
        f"Location: {job.location}\n\nDescription:\n{(job.description or '')[:4000]}\n\n"
        f"=== BASE RÉSUMÉ (the source of truth — every claim must trace back here) ===\n"
        f"{base_text}"
    )
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model=settings.resume_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("AI résumé build failed for '%s', falling back to base: %s",
                    job.title, exc)
        return ""


def build(job: Job) -> tuple[str, bool]:
    """Return (resume_markdown, used_ai). Falls back to the untouched base
    résumé (already true) when AI is off or fails."""
    base = resolve_base(job.title)
    base_text = _load_base(base)
    if settings.resume_ai_enabled and base_text:
        ai = _ai_build(job, base_text)
        if ai:
            return ai, True
    return base_text, False


def save(job: Job, resume_md: str) -> dict:
    """Write the résumé to resumes/generated/ as .md (readable) and .txt (the
    plain single-column form ATS parsers handle best). Returns paths + text."""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{job.id}_{_slug(job.company_name)}"
    md_path = GENERATED_DIR / f"{stem}.md"
    txt_path = GENERATED_DIR / f"{stem}.txt"
    docx_path = GENERATED_DIR / f"{stem}.docx"
    tex_path = GENERATED_DIR / f"{stem}.tex"
    pdf_path = GENERATED_DIR / f"{stem}.pdf"
    resume_txt = _md_to_text(resume_md)
    md_path.write_text(resume_md, encoding="utf-8")
    txt_path.write_text(resume_txt, encoding="utf-8")
    has_docx = _to_docx(resume_md, docx_path)
    # Jake's Resume LaTeX → PDF (the polished, ATS-friendly upload artifact).
    tex = latex_resume.render_tex(resume_md)
    tex_path.write_text(tex, encoding="utf-8")
    has_pdf = latex_resume.compile_pdf(tex, pdf_path)
    return {
        "md_path": str(md_path),
        "txt_path": str(txt_path),
        "docx_path": str(docx_path) if has_docx else None,
        "tex_path": str(tex_path),
        "pdf_path": str(pdf_path) if has_pdf else None,
        "resume": resume_md,
        "resume_txt": resume_txt,
    }


def get_profile() -> dict:
    """Parse the standard contact/identity fields from the base data résumé so
    the dashboard can offer a one-click copyable autofill kit. Falls back to
    whatever it can find; never raises."""
    text = _load_base(resolve_base("data engineer"))
    def grab(pattern: str) -> str:
        m = re.search(pattern, text, re.I)
        return (m.group(1).strip() if m else "")
    name = ""
    m = re.search(r"^#\s*(.+)$", text, re.M)
    if m:
        name = re.split(r"[—\-|]", m.group(1))[0].strip()
    return {
        "name": name,
        "email": grab(r"Email:\**\s*([^\s·|]+@[^\s·|]+)"),
        "phone": grab(r"Phone:\**\s*([0-9()+\-.\s]{7,})"),
        "linkedin": grab(r"(linkedin\.com/[^\s·|)]+)"),
        "location": grab(r"Location:\**\s*([^·\n|]+)"),
        "work_authorization": settings.my_work_auth,
        "target_roles": ", ".join(settings.target_roles_list),
        "top_skills": ", ".join(settings.skills_list[:12]),
    }


def load_saved(job_id: int) -> dict | None:
    """Return the previously-built résumé for a job (the durable reference you
    keep 'in case you get a call'), or None if none was built. Reads the files
    in resumes/generated/ keyed by job id."""
    mds = sorted(GENERATED_DIR.glob(f"{job_id}_*.md"))
    if not mds:
        return None
    md_path = mds[0]
    txt_path = md_path.with_suffix(".txt")
    try:
        resume_md = md_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    resume_txt = txt_path.read_text(encoding="utf-8") if txt_path.exists() else _md_to_text(resume_md)
    docx_path = md_path.with_suffix(".docx")
    pdf_path = md_path.with_suffix(".pdf")
    tex_path = md_path.with_suffix(".tex")
    return {
        "md_path": str(md_path),
        "txt_path": str(txt_path),
        "docx_path": str(docx_path) if docx_path.exists() else None,
        "tex_path": str(tex_path) if tex_path.exists() else None,
        "pdf_path": str(pdf_path) if pdf_path.exists() else None,
        "resume": resume_md,
        "resume_txt": resume_txt,
    }


def build_and_save(job: Job) -> dict:
    resume_md, used_ai = build(job)
    if not resume_md:
        raise RuntimeError(
            "No base résumé found in resumes/ — expected base_data_engineer.md etc."
        )
    out = save(job, resume_md)
    out["used_ai"] = used_ai
    out["login_free"] = is_login_free(job.source)
    out["base"] = resolve_base(job.title)
    return out
