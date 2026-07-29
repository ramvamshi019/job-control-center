"""
utils/ats.py
------------
Single source of truth for ATS URL detection. Before this module, the
same regex list was duplicated across:

  scripts/seed_hn_who_is_hiring.py
  scripts/seed_hn_job_stories.py
  scripts/seed_yc.py         (via auto_discover)
  scripts/gmail_watcher.py   (indirectly, via domain matching)
  dashboard/app.py           (in referral / email-domain guessing)

One typo in a copy = a silent bug in that specific code path. Now every
seeder + dashboard imports from here. Add a new ATS in ONE place.

The two exported names:
  ATS_PATTERNS   -- list of (ats_name, compiled_regex) for detecting a
                    known ATS host + slug from an HN comment / job URL.
                    Regex group 1 is the tenant slug.
  detect_ats(text) -> (ats_name, slug) | None
                    Convenience: run every pattern, return the first hit.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# The one canonical list. Order matters only when patterns overlap -- keep
# more-specific ones first. Group 1 = tenant slug in every entry.
ATS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse",      re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)", re.I)),
    ("lever",           re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_-]+)", re.I)),
    ("ashby",           re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("workday",         re.compile(r"([a-zA-Z0-9_-]+)\.wd\d+\.myworkdayjobs\.com", re.I)),
    ("icims",           re.compile(r"careers[-_]?([a-zA-Z0-9_-]+)\.icims\.com", re.I)),
    ("bamboohr",        re.compile(r"([a-zA-Z0-9_-]+)\.bamboohr\.com", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("workable",        re.compile(r"apply\.workable\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("rippling",        re.compile(r"ats\.rippling\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("recruitee",       re.compile(r"([a-zA-Z0-9_-]+)\.recruitee\.com", re.I)),
    ("gem",             re.compile(r"gem\.com/careers/([a-zA-Z0-9_-]+)", re.I)),
]

# Hosts that are the ATS itself (not the employer). Used by the domain
# guesser to skip them when looking up a company's own domain for follow-up
# email routing. Keeping this list next to ATS_PATTERNS so both stay in sync.
ATS_HOSTS: tuple[str, ...] = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "icims.com", "bamboohr.com", "smartrecruiters.com", "workable.com",
    "rippling.com", "recruitee.com", "himalayas.app", "jobvite.com",
    "gem.com", "workday.com", "eightfold.ai", "themuse.com",
)


def detect_ats(text: str) -> Optional[Tuple[str, str]]:
    """Return (ats_name, slug) for the first ATS URL found in `text`.
    None if no known ATS matches.  Used by every URL-parsing seeder."""
    if not text:
        return None
    for name, pat in ATS_PATTERNS:
        m = pat.search(text)
        if m:
            slug = m.group(1)
            if slug and len(slug) < 60:
                return (name, slug)
    return None


def is_ats_host(host: str) -> bool:
    """True if the given URL host is one of the known ATS platforms
    (i.e. not the employer's own domain). Used for domain-guessing when
    building careers@<domain> email addresses for follow-ups."""
    host = (host or "").lower().lstrip(".")
    return any(a in host for a in ATS_HOSTS)
