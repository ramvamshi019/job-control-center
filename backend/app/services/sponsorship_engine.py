"""
services/sponsorship_engine.py
------------------------------
Estimates visa-sponsorship risk for an F-1/OPT candidate who may need H-1B.

`assess(job, company)` returns (risk:str, risk_reason:str) where risk is one of:
    "reject"  -> explicit blocker (citizenship/clearance/no sponsorship)
    "high"    -> no confirmed sponsor history + vague authorization language
    "medium"  -> unclear language OR unconfirmed company sponsorship
    "low"     -> confirmed H-1B sponsor (or sponsor-friendly posting language)

h1b_history_score is set by scripts/enrich_h1b.py from USCIS data: 40=unknown,
45=employer matched but zero approvals, 65/78/88/95=confirmed sponsor (rising
with approval volume). The >=50 cutoff for "low" therefore means a confirmed
sponsor and matches the scoring engine's bonus threshold.

This is a HEURISTIC, not legal advice. It only reads the posting text + the
company's h1b_history_score. Always verify before applying.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from app.models.company import Company
from app.models.job import Job
from app.utils.text import normalize

REJECT_PHRASES = [
    "no visa sponsorship", "we do not sponsor", "we are unable to sponsor",
    "sponsorship not available", "unable to sponsor",
    "must be authorized to work without sponsorship",
    "must be authorized to work in the u.s. without sponsorship",
    "now or in the future without sponsorship",
    "without the need for sponsorship",
    # --- US citizenship required (dead-ends for an F-1/OPT candidate) ---
    "us citizen required", "u.s. citizen required", "must be a us citizen",
    "must be us citizen", "must be a u.s. citizen",
    "citizenship is required", "citizenship required",
    "us citizenship is required", "u.s. citizenship is required",
    "us citizenship required", "u.s. citizenship required",
    "requires us citizenship", "requires u.s. citizenship",
    "must hold us citizenship", "must hold u.s. citizenship",
    "us citizens only", "u.s. citizens only",
    "must be a us person", "must be a u.s. person",
    "us persons only", "u.s. persons only",
    # --- security clearance (require citizenship; not obtainable on F-1) ---
    "security clearance", "active clearance", "clearance required",
    "secret clearance", "top secret", "ts/sci",
    "active security clearance", "current security clearance",
    "ability to obtain a security clearance",
    "able to obtain a security clearance",
    "eligible for a security clearance",
]

# Language that's vague but not an outright "no".
VAGUE_PHRASES = [
    "must be authorized to work in the united states",
    "authorized to work in the us",
    "eligible to work in the united states",
    "work authorization",
]

GOOD_PHRASES = [
    "visa sponsorship available", "we sponsor", "will sponsor",
    "sponsorship available", "h-1b", "h1b", "opt", "cpt",
]

# --- Flexible "we won't sponsor" detection ----------------------------------
# Exact-substring REJECT_PHRASES miss the real-world phrasings because words sit
# between the keywords, e.g. AIR's "visa sponsorship (including H-1B sponsorship)
# IS NOT available for this position", or "we do not offer sponsorship". These
# regexes match a negation next to a sponsorship mention WITHIN ONE sentence
# (bounded gap, no sentence-ender in between). The verb form ("...sponsor...") is
# additionally scoped to a visa context so benign "sponsor relocation / sponsor a
# 5K" lines don't trip it. Positive language ("happy to sponsor visas") has no
# adjacent negation, so it is never matched.
# NOTE the \b around the whole group: without it, "no" matches inside "now"
# ("will you NOW or in the future require sponsorship" is a NEUTRAL question, not
# a blocker) and "not" matches inside "notable", etc.
_NEG = (
    r"\b(?:no|not|never|without|unable|cannot|can\s?not|won'?t|will\s+not|"
    r"do(?:es)?\s+not|are\s+not|is\s+not|aren'?t|isn'?t|not\s+able|"
    r"not\s+eligible|not\s+available|unavailable|not\s+offered|not\s+provided)\b"
)
_VISA = (
    r"(?:visa|h-?1b|h1-b|work\s+authoriz|employment\s+authoriz|immigration|"
    r"green\s+card|permanent\s+resident|work\s+permit|for\s+(?:this|employment|work))"
)
# Reverse direction ("sponsorship ... <negation>") is restricted to UNavailability
# wording — bare "not" there is too loose ("sponsorship nice to have (not
# required)" / "sponsorship is not a requirement" are NOT blockers).
_NEG_UNAVAIL = (
    r"(?:is\s+)?(?:not\s+available|unavailable|not\s+offered|not\s+provided|"
    r"not\s+possible|not\s+an\s+option|not\s+eligible|cannot\s+be\s+(?:offered|provided))"
)
_GAP = r"[^.;:\n]{0,30}"
REJECT_REGEXES = [
    # negation ... sponsorship(noun)
    re.compile(_NEG + _GAP + r"\bsponsorship\b", re.I),
    # sponsorship(noun) ... is not available / unavailable / not offered ...
    re.compile(r"\bsponsorship\b" + _GAP + _NEG_UNAVAIL, re.I),
    # negation ... sponsor(verb) ... visa-context   (either order)
    re.compile(_NEG + r"[^.;:\n]{0,20}\bsponsor\b[^.;:\n]{0,40}" + _VISA, re.I),
    re.compile(_VISA + r"[^.;:\n]{0,40}" + _NEG + r"[^.;:\n]{0,20}\bsponsor\b", re.I),
    # "must be authorized to work ... without ... sponsor(ship)"
    re.compile(
        r"authorized\s+to\s+work[^.;:\n]{0,40}without[^.;:\n]{0,20}sponsor", re.I
    ),
]

# "sponsorship" that ISN'T visa sponsorship — don't reject on these.
_NONVISA_SPONSOR = re.compile(
    r"\b(?:relocation|tuition|exam|certification|certificate|membership|event|"
    r"conference|gala|marathon|charity|league|golf|booth|housing|travel)\b", re.I)
_VISA_TOKEN = re.compile(
    r"\b(?:visa|h-?1b|h1-b|immigration|work\s+authoriz|employment\s+authoriz|"
    r"work\s+permit|green\s+card|permanent\s+resident)\b", re.I)
# Form-instruction phrasing ("answer 'no' only if you ...") is neutral, not a
# disclosure — these leak in when scanning a full application page.
_NEUTRAL_CTX = re.compile(
    r"\bonly\s+if\b|\banswer\b|\bselect\b|\bindicate\b|\bplease\s+choose\b", re.I)


def _is_real_visa_blocker(ctx: str) -> bool:
    """ctx = the match plus its same-sentence preceding words. A qualifier like
    "tuition"/"relocation" often sits BEFORE the matched 'sponsorship', so we
    judge on the wider (but same-sentence) context."""
    if _NEUTRAL_CTX.search(ctx):
        return False
    if _NONVISA_SPONSOR.search(ctx) and not _VISA_TOKEN.search(ctx):
        return False  # e.g. "relocation sponsorship", "tuition sponsorship"
    return True


def no_sponsorship(text: str) -> Optional[str]:
    """Return the matched blocker snippet if the text says they WON'T sponsor a
    work VISA, else None. Skips non-visa "sponsorship" (relocation/tuition/event)
    and neutral form-instruction text. Operates on normalized text."""
    for rx in REJECT_REGEXES:
        for m in rx.finditer(text):
            # Same-sentence preceding context (don't cross . ; : newline, so a
            # visa mention in a PRIOR sentence can't mask a non-visa match here).
            pre = re.split(r"[.;:\n]", text[max(0, m.start() - 30):m.start()])[-1]
            if _is_real_visa_blocker(pre + m.group(0)):
                return m.group(0).strip()
    return None


def assess(job: Job, company: Optional[Company] = None) -> Tuple[str, str]:
    desc = normalize(job.description)
    history = company.h1b_history_score if company else 0

    # 1) Explicit blockers => reject. Check TITLE + description, since many
    # citizenship/clearance roles flag it only in the title (e.g. "... -
    # Clearance Required") which a description-only scan would miss.
    hay = normalize(job.title) + " " + desc
    for p in REJECT_PHRASES:
        if p in hay:
            return "reject", f"Explicit blocker in posting: '{p}'"

    # Flexible no-sponsorship phrasings the literal list can't catch.
    snippet = no_sponsorship(hay)
    if snippet:
        return "reject", f"Posting states no visa sponsorship: '{snippet}'"

    # 2) Explicit positive language => low risk.
    for p in GOOD_PHRASES:
        if p in desc:
            return "low", f"Posting mentions sponsorship-friendly language: '{p}'"

    # 3) Confirmed sponsor (enriched score >= 50) with no blockers => low.
    if history >= 50:
        return "low", f"Confirmed H-1B sponsor (USCIS history score {history})"

    # 4) Matched-but-weak (45) or default-unknown (40) => medium, verify.
    if history >= 30:
        note = "matched in USCIS data but no recent approvals" if history >= 45 else "no confirmed sponsorship history"
        return "medium", f"Unconfirmed sponsor ({note}, score {history}); verify before applying"

    # 5) Vague authorization language + no history => high.
    if any(p in desc for p in VAGUE_PHRASES):
        return "high", "Vague work-authorization language and no known sponsorship history"

    # 6) Nothing known either way => medium (needs human review).
    return "medium", "No sponsorship language and unknown company history — review manually"
