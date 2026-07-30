"""
routes/apply_ai.py
------------------
Backs the Chrome extension's AI-suggest fallback for custom application-form
questions (screening questions unique per company/JD).

POST /apply/suggest_answer
  input:
    question   str    - the visible field label ("Are you willing to relocate?")
    options    [str]  - for radio/select, the option labels to choose from
    kind       str    - 'text' | 'select' | 'radio'
    job_title  str?
    company    str?
    profile    dict   - full Chrome-extension profile (name, email, F-1 OPT flags, …)
  output:
    answer     str    - the value to fill (for radio/select, one of `options` verbatim)
    confidence int    - Claude's 0-100 self-rated confidence
    reasoning  str    - one-sentence why (surfaced as a tooltip in the extension)

Rate-limited implicitly by the extension's 8-per-page-load cap. Cost per
call ~$0.002 (short prompt, ~120 tokens output). Silent no-op when
ANTHROPIC_API_KEY is absent so extension gracefully falls back to blank.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.utils.logging import get_logger

router = APIRouter(prefix="/apply", tags=["apply_ai"])
log = get_logger("apply_ai")


class SuggestIn(BaseModel):
    question: str
    options: Optional[List[str]] = None
    kind: Optional[str] = "text"
    job_title: Optional[str] = None
    company: Optional[str] = None
    profile: Optional[dict] = None


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


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


def _profile_line(p: dict | None) -> str:
    if not p:
        return "(no profile)"
    def g(k, default=""):
        v = (p.get(k) or "").strip() if isinstance(p.get(k), str) else p.get(k)
        return str(v) if v not in (None, "") else default
    return (
        f"name={g('fullName') or (g('firstName') + ' ' + g('lastName')).strip()}, "
        f"email={g('email')}, phone={g('phone')}, "
        f"linkedin={g('linkedin')}, "
        f"city={g('city')}, state={g('state')}, country={g('country', 'United States')}, "
        f"years_experience={g('yearsExperience', '2')}, "
        f"work_authorized_us={g('workAuthYes', 'Yes')} (F-1 OPT STEM), "
        f"needs_sponsorship={g('sponsorRequiredYes', 'Yes')}"
    )


@router.post("/suggest_answer")
def suggest_answer(payload: SuggestIn):
    if settings.ai_provider != "anthropic" or not settings.anthropic_api_key:
        return {"answer": "", "confidence": 0, "reasoning": "AI provider not configured"}
    if not payload.question or len(payload.question) < 3:
        return {"answer": "", "confidence": 0, "reasoning": "empty question"}

    prof_line = _profile_line(payload.profile)
    options_block = ""
    format_hint = ""
    if payload.options:
        options_block = "Options (pick EXACTLY ONE, verbatim):\n" + \
            "\n".join(f"  - {o}" for o in payload.options[:40])
        format_hint = "answer MUST be one of the options above, verbatim (case + whitespace exact)"
    else:
        format_hint = "answer is a short free-text string (1-40 chars typical)"

    prompt = f"""You're filling out a job application on behalf of a candidate. Pick the honest, professional answer they would give.

Candidate profile: {prof_line}
Applying to: {payload.job_title or '(unknown role)'} at {payload.company or '(unknown company)'}

Question: "{payload.question}"
{options_block}

Rules:
- Truthful — never lie about work auth, sponsorship, citizenship, clearances.
- Match the candidate's actual profile above.
- For "willing to relocate" default YES unless the profile says otherwise.
- For "start date" prefer "Immediately" or "2 weeks notice".
- For "salary expectation" leave blank (return "" as answer) — this is strategic.
- For voluntary EEO/demographic questions leave blank (return "").
- {format_hint}

Return ONLY a JSON object, no preface, no markdown:
{{
  "answer": "<string>",
  "confidence": <int 0-100>,
  "reasoning": "<one sentence why>"
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=250,
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
        parsed = _extract_json(raw) or {}
        answer = (parsed.get("answer") or "").strip()
        confidence = int(parsed.get("confidence") or 0)
        reasoning = (parsed.get("reasoning") or "").strip()[:200]
        # Validate against options if provided — never invent an option
        if payload.options and answer:
            match = next(
                (o for o in payload.options if o.strip().lower() == answer.lower()),
                None,
            )
            if match:
                answer = match  # snap to verbatim
            else:
                # loose contains match
                match = next(
                    (o for o in payload.options
                     if answer.lower() in o.lower() or o.lower() in answer.lower()),
                    None,
                )
                if match:
                    answer = match
                else:
                    log.info("suggest: answer %r not in options — dropping", answer[:60])
                    answer = ""
        return {"answer": answer, "confidence": confidence, "reasoning": reasoning}
    except Exception as e:  # noqa: BLE001
        log.warning("suggest_answer: Claude call failed: %s", e)
        return {"answer": "", "confidence": 0, "reasoning": f"error: {e}"}
