"""
services/latex_resume.py
------------------------
Render a tailored résumé (Markdown produced by resume_builder) into **Jake's
Resume** LaTeX format and compile it to PDF with `tectonic`.

Jake's Resume (github.com/jakegut/resume) is a deterministic, well-known
template, so we reproduce its preamble + custom commands here and pour the
tailored content into it — the output matches that template's look. Nothing is
invented; we only typeset whatever Markdown resume_builder already produced.

If `tectonic` isn't on PATH (or compilation fails), compile_pdf returns False
and the caller keeps the .md/.txt/.docx artifacts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.utils.logging import get_logger

log = get_logger("latex_resume")


def _tectonic() -> str | None:
    """Resolve the tectonic binary. Checks PATH first, then common install
    locations — launchd services run with a minimal PATH that omits Homebrew."""
    found = shutil.which("tectonic")
    if found:
        return found
    for p in ("/opt/homebrew/bin/tectonic", "/usr/local/bin/tectonic", "/usr/bin/tectonic"):
        if os.path.exists(p):
            return p
    return None

# --- Jake's Resume preamble + custom commands (faithful reproduction) ---
PREAMBLE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}

\pagestyle{fancy}
\fancyhf{}
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}

\addtolength{\oddsidemargin}{-0.5in}
\addtolength{\evensidemargin}{-0.5in}
\addtolength{\textwidth}{1in}
\addtolength{\topmargin}{-.5in}
\addtolength{\textheight}{1.0in}

\urlstyle{same}
\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}

\titleformat{\section}{
  \vspace{-4pt}\scshape\raggedright\large
}{}{0em}{}[\color{black}\titlerule \vspace{-5pt}]

\newcommand{\resumeItem}[1]{
  \item\small{
    {#1 \vspace{-2pt}}
  }
}

\newcommand{\resumeSubheading}[4]{
  \vspace{-2pt}\item
    \begin{tabular*}{0.97\textwidth}[t]{l@{\extracolsep{\fill}}r}
      \textbf{#1} & #2 \\
      \textit{\small#3} & \textit{\small #4} \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeSubItem}[1]{\resumeItem{#1}\vspace{-4pt}}
\renewcommand\labelitemii{$\vcenter{\hbox{\tiny$\bullet$}}$}
\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-5pt}}
"""


def _esc(s: str) -> str:
    """Escape LaTeX special characters in plain text."""
    repl = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
        "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    out = []
    for ch in s:
        out.append(repl.get(ch, ch))
    return "".join(out)


def _inline(s: str) -> str:
    """Escape text and render **bold** as \\textbf{...}."""
    parts = re.split(r"\*\*(.+?)\*\*", s)
    rendered = []
    for i, chunk in enumerate(parts):
        esc = _esc(chunk)
        rendered.append(f"\\textbf{{{esc}}}" if i % 2 == 1 else esc)
    return "".join(rendered)


def _parse_header(lines: list[str]) -> tuple[dict, int]:
    """Pull name + contact fields from the leading block. Returns (header, idx
    of first '## ' section)."""
    h = {"name": "", "title": "", "phone": "", "email": "", "linkedin": "", "location": ""}
    i = 0
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return h, i
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            h["name"] = re.split(r"\s[—\-|]\s", m.group(1))[0].strip()
            continue
        for key, pat in (("title", r"Title:\**\s*(.+)"),
                         ("location", r"Location:\**\s*([^·|\n]+)"),
                         ("phone", r"Phone:\**\s*([0-9()+\-.\s]{7,})"),
                         ("email", r"Email:\**\s*([^\s·|]+@[^\s·|]+)"),
                         ("linkedin", r"(linkedin\.com/[^\s·|)]+)")):
            mm = re.search(pat, line, re.I)
            if mm and not h[key]:
                h[key] = mm.group(1).strip()
    return h, len(lines)


def _render_header(h: dict) -> str:
    contacts = []
    if h["location"]:
        contacts.append(_esc(h["location"]))
    if h["phone"]:
        contacts.append(_esc(h["phone"]))
    if h["email"]:
        contacts.append(f"\\href{{mailto:{h['email']}}}{{\\underline{{{_esc(h['email'])}}}}}")
    if h["linkedin"]:
        url = h["linkedin"] if h["linkedin"].startswith("http") else "https://" + h["linkedin"]
        contacts.append(f"\\href{{{url}}}{{\\underline{{{_esc(h['linkedin'])}}}}}")
    line2 = " $|$ ".join(contacts)
    title = f"\\\\ \\vspace{{2pt}} \\small {_esc(h['title'])}" if h["title"] else ""
    return (
        "\\begin{center}\n"
        f"    {{\\Huge \\scshape {_esc(h['name'])}}} {title} \\\\ \\vspace{{3pt}}\n"
        f"    \\small {line2}\n"
        "\\end{center}\n"
    )


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    sections, name, body = [], None, []
    for line in lines:
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if name is not None:
                sections.append((name, body))
            name, body = m.group(1).strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        sections.append((name, body))
    return sections


def _render_experience(body: list[str]) -> str:
    """### Role — Company · Dates  /  *Location*  /  - bullets → resumeSubheading."""
    out = ["\\resumeSubHeadingListStart"]
    i, n = 0, len(body)
    while i < n:
        line = body[i].strip()
        h = re.match(r"^###\s+(.*)$", line)
        if not h:
            i += 1
            continue
        head = h.group(1)
        role, company, dates = head, "", ""
        if " — " in head:
            role, rest = head.split(" — ", 1)
            if " · " in rest:
                company, dates = rest.rsplit(" · ", 1)
            else:
                company = rest
        location = ""
        i += 1
        if i < n and body[i].strip().startswith("*") and body[i].strip().endswith("*"):
            location = body[i].strip().strip("*").strip()
            i += 1
        out.append(f"  \\resumeSubheading{{{_inline(role.strip())}}}{{{_esc(dates.strip())}}}"
                   f"{{{_inline(company.strip())}}}{{{_esc(location)}}}")
        items = []
        while i < n:
            b = re.match(r"^\s*[-*+]\s+(.*)$", body[i])
            if b:
                items.append(b.group(1))
                i += 1
            elif body[i].strip() == "" or body[i].startswith("###"):
                break
            else:
                i += 1
        if items:
            out.append("    \\resumeItemListStart")
            for it in items:
                out.append(f"      \\resumeItem{{{_inline(it)}}}")
            out.append("    \\resumeItemListEnd")
    out.append("\\resumeSubHeadingListEnd")
    return "\n".join(out)


def _render_bullets_block(body: list[str]) -> str:
    """Skills / generic bullet list: label={} itemize with one \\item per bullet."""
    bullets = [re.match(r"^\s*[-*+]\s+(.*)$", l).group(1)
               for l in body if re.match(r"^\s*[-*+]\s+(.*)$", l)]
    if not bullets:
        text = " ".join(l.strip() for l in body if l.strip())
        return f"{_inline(text)}\n" if text else ""
    out = ["\\begin{itemize}[leftmargin=0.15in, label={}]"]
    for b in bullets:
        out.append(f"  \\small{{\\item{{{_inline(b)}}}}}")
    out.append("\\end{itemize}")
    return "\n".join(out)


def render_tex(resume_md: str) -> str:
    lines = resume_md.splitlines()
    header, start = _parse_header(lines)
    body_lines = lines[start:]
    parts = [PREAMBLE, "\\begin{document}\n", _render_header(header)]
    for name, body in _split_sections(body_lines):
        parts.append(f"\n\\section{{{_esc(name)}}}")
        if any(re.match(r"^###\s+", l) for l in body):
            parts.append(_render_experience(body))
        else:
            parts.append(_render_bullets_block(body))
    parts.append("\n\\end{document}\n")
    return "\n".join(parts)


def compile_pdf(tex: str, out_pdf: Path) -> bool:
    """Compile LaTeX → PDF with tectonic. Returns True on success."""
    binary = _tectonic()
    if not binary:
        log.warning("tectonic not found; skipping PDF")
        return False
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "resume.tex").write_text(tex, encoding="utf-8")
            proc = subprocess.run(
                [binary, "resume.tex", "--outdir", str(tdp), "--keep-logs"],
                cwd=str(tdp), capture_output=True, text=True, timeout=120,
            )
            pdf = tdp / "resume.pdf"
            if proc.returncode != 0 or not pdf.exists():
                log.warning("tectonic failed (%s): %s", proc.returncode, proc.stderr[-600:])
                return False
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf, out_pdf)
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF compile error: %s", exc)
        return False
