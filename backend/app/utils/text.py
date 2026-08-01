"""
utils/text.py
-------------
Small text helpers used by crawlers and engines.
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup


def clean_html(raw: str) -> str:
    """Strip HTML tags, collapse whitespace. Safe on plain text too."""
    if not raw:
        return ""
    try:
        text = BeautifulSoup(raw, "lxml").get_text(" ")
    except Exception:
        # Fallback if lxml/bs4 trips on weird input.
        text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize(text: str) -> str:
    """Lowercase + squash whitespace, for keyword matching."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def term_in(text: str, term: str) -> bool:
    """True if `term` appears in `text` as a whole token, not a substring.

    Avoids false hits like 'java' matching 'javascript' or 'scala' matching
    'scalable'. Boundaries are non-alphanumeric, so multi-word terms
    ('azure data factory') and punctuated ones ('pl/sql', 'ci/cd') work.
    Inputs are assumed already lowercased (see `normalize`).
    """
    if not text or not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def make_hash(*parts: str) -> str:
    """Stable hash from any number of strings. Used for dedupe."""
    joined = "||".join(normalize(p) for p in parts if p is not None)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# Legal suffixes/prefixes that show up on some sources but not others, causing
# the same employer to look like two ("Stripe" vs "Stripe, Inc." vs "Stripe Inc").
_COMPANY_SUFFIX_RE = re.compile(
    r",?\s*(inc\.?|llc|l\.l\.c\.|ltd\.?|limited|corp\.?|corporation|co\.|company|plc|"
    r"gmbh|s\.a\.?|s\.p\.a\.?|holdings?|group|technologies|technology|tech|labs?|"
    r"solutions?|services?|systems?|international|worldwide)\.?$",
    re.I,
)

# Boilerplate words that clutter titles without changing the role identity.
# Example: "Senior Data Engineer II" vs "Senior Data Engineer 2" vs "Data Engineer, Senior"
# — all the same posting when a company reshuffles wording between crawls / feeds.
_TITLE_NOISE_RE = re.compile(
    r"\b(remote|onsite|on-site|hybrid|full[-\s]?time|part[-\s]?time|contract|contractor|"
    r"temporary|temp|permanent|new\s+grad|entry[-\s]?level|senior|sr\.?|junior|jr\.?|"
    r"principal|staff|lead|iii|ii|iv|i|1|2|3|4)\b",
    re.I,
)

_ROMAN_TO_ARABIC = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}


def _clean_company(name: str) -> str:
    s = normalize(name)
    s = _COMPANY_SUFFIX_RE.sub("", s).strip(" ,.")
    return s


def _clean_title(title: str) -> str:
    s = normalize(title)
    # Split on comma/pipe and drop trailing " - City" tails that some sources include.
    s = re.sub(r"\s*[-–—|,]\s+(remote|hybrid|onsite|on-site|us|usa|united states|"
               r"[a-z ]+, [a-z]{2})\s*$", "", s)
    s = _TITLE_NOISE_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _clean_location(location: str) -> str:
    """Reduce a location to a comparable primary anchor.
    "Remote, US" / "United States (Remote)" / "US-Remote" all collapse to 'remote-us'.
    "San Francisco, CA / New York, NY" keeps only the first city (dedupe is per-posting,
    not per-office)."""
    s = normalize(location)
    if not s:
        return ""
    # Remote-US variants collapse to a single anchor.
    if re.search(r"\bremote\b", s) and re.search(r"\b(us|usa|united states|u\.s\.)\b", s):
        return "remote-us"
    if re.fullmatch(r"remote", s):
        return "remote"
    # Take the first location if a list.
    first = re.split(r"\s*[/;]\s*|\s*\bor\b\s*", s)[0]
    # Drop parenthetical modifiers ("(Hybrid)").
    first = re.sub(r"\s*\([^)]*\)\s*", " ", first).strip()
    return first


def canonical_job_key(company_name: str, title: str, location: str) -> str:
    """Cross-source dedupe key. Two rows with the same key are the same real
    posting seen via different feeds (Greenhouse + Simplify + HN + Himalayas).
    Normalizes legal suffixes ("Stripe Inc" == "Stripe"), title noise
    ("Sr Data Engineer II" == "Data Engineer"), and location variants
    ("Remote, US" == "US-Remote"). Empty inputs collapse to '' (no dedupe)."""
    c = _clean_company(company_name)
    t = _clean_title(title)
    L = _clean_location(location)
    if not c or not t:
        return ""  # too little signal to dedupe safely
    return hashlib.sha256(f"{c}||{t}||{L}".encode("utf-8")).hexdigest()


# Sentences mentioning any of these decide whether an F-1/OPT candidate can even
# apply (work-authorization disclosures). They almost always sit at the very
# BOTTOM of a long posting — past the length cap — so a naive truncate drops them
# and the sponsorship engine never sees them. We preserve them explicitly.
_DISCLOSURE_HINTS = re.compile(
    r"sponsor|visa|h-?1b|h1-b|citizen|clearance|work\s+authoriz|employment\s+authoriz|"
    r"authorized\s+to\s+work|green\s+card|permanent\s+resident|u\.?s\.?\s+person|"
    r"export\s+control|itar",
    re.I,
)


def truncate(text: str, limit: int = 6000) -> str:
    """Keep descriptions from blowing up the DB / AI prompts — but NEVER drop a
    visa/sponsorship/citizenship/clearance disclosure. Those usually live at the
    bottom of a long JD (past the cap); losing them is why dead-end "we don't
    sponsor" roles were scoring as top matches. So when we cut, we scan the
    dropped tail and re-append the disclosure sentences (capped, deduped)."""
    text = text or ""
    if len(text) <= limit:
        return text
    head, tail = text[:limit], text[limit:]
    out = head + " …"
    # Pull disclosure sentences out of the dropped tail. Split on sentence
    # enders + newlines; keep only the ones that carry a work-auth signal.
    seen: set[str] = set()
    picks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", tail):
        s = sentence.strip()
        if not s or not _DISCLOSURE_HINTS.search(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        picks.append(s)
        if len(picks) >= 4:  # a couple of lines is all the engine needs
            break
    if picks:
        out += " [work-authorization notice] " + " ".join(picks)
    return out
