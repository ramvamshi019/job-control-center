"""
utils/ats_sniff.py
------------------
Given ANY careers URL, follow redirects and inspect the landing page to
identify the underlying ATS. Returns (ats_type, career_url_for_seed) or
(None, None) when nothing matches.

The `career_url_for_seed` is the exact string to store in Company.career_url
— it varies per ATS (bare token for greenhouse/lever, pipe form for workday,
full host for phenom/eightfold, etc.), matching what each crawler's
extract_token / can_handle expects.

No probing — just one HTTP GET per URL. Cheap and parallelizable.

Ordering matters: more-specific vendor markers checked before generic ones,
since some careers pages are wrapped by another vendor (e.g. an Eightfold
site iframed inside a corporate host).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests

TIMEOUT = 10
HEADERS = {
    "User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)",
    "Accept": "text/html,application/xhtml+xml,*/*",
}


def _get(url: str) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r
    except Exception:  # noqa: BLE001
        return None


def _host_and_body(r: requests.Response) -> Tuple[str, str, str]:
    final_url = r.url
    host = (urlparse(final_url).hostname or "").lower()
    body = r.text[:200000]  # cap for grep speed
    return final_url, host, body


def sniff(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (ats_type, career_url_for_seed) or (None, None)."""
    s = (url or "").strip()
    if not s:
        return None, None
    if not s.startswith("http"):
        s = "https://" + s

    # Sometimes the redirect target ALONE is enough (short-circuit).
    r = _get(s)
    if not r:
        return None, None
    final_url, host, body = _host_and_body(r)

    # ---- URL-host matches (highest signal) ----
    # Greenhouse
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "greenhouse", m.group(1)

    # Lever
    m = re.search(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "lever", m.group(1)

    # Ashby
    m = re.search(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", body + " " + final_url)
    if m:
        return "ashby", m.group(1)

    # SmartRecruiters
    m = re.search(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "smartrecruiters", m.group(1)
    m = re.search(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "smartrecruiters", m.group(1)

    # BambooHR
    m = re.search(r"([A-Za-z0-9_-]+)\.bamboohr\.com/(?:careers|jobs)", body + " " + final_url)
    if m:
        return "bamboohr", m.group(1)

    # Workable
    m = re.search(r"apply\.workable\.com/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "workable", m.group(1)

    # Recruitee
    m = re.search(r"([A-Za-z0-9_-]+)\.recruitee\.com", body + " " + final_url)
    if m:
        return "recruitee", m.group(1)

    # Workday — needs host + tenant path, pipe-encode for the crawler.
    if "myworkdayjobs.com" in host or "myworkdayjobs.com" in body:
        m = re.search(r"([a-z0-9-]+\.(?:wd\d+\.)?myworkdayjobs\.com)/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)", body + " " + final_url)
        if m:
            return "workday", f"{m.group(1)}|{m.group(2)}|{m.group(3)}"

    # iCIMS
    m = re.search(r"careers-([A-Za-z0-9_-]+)\.icims\.com|([A-Za-z0-9_-]+)\.icims\.com/jobs/search", body + " " + final_url)
    if m:
        tok = m.group(1) or m.group(2)
        return "icims", tok

    # Rippling
    m = re.search(r"ats\.rippling\.com/([A-Za-z0-9_-]+)/jobs?", body + " " + final_url)
    if m:
        return "rippling", m.group(1)

    # Breezy
    m = re.search(r"([A-Za-z0-9_-]+)\.breezy\.hr", body + " " + final_url)
    if m:
        return "breezy", m.group(1)

    # Personio
    m = re.search(r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:com|de)", body + " " + final_url)
    if m:
        return "personio", m.group(1)

    # Teamtailor
    m = re.search(r"([A-Za-z0-9_-]+)\.teamtailor\.com", body + " " + final_url)
    if m:
        return "teamtailor", m.group(1)

    # Pinpoint
    m = re.search(r"([A-Za-z0-9_-]+)\.pinpointhq\.com", body + " " + final_url)
    if m:
        return "pinpoint", m.group(1)

    # Jobvite
    m = re.search(r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "jobvite", m.group(1)

    # Paylocity
    m = re.search(r"recruiting\.paylocity\.com/recruiting/jobs/all/(\d+)/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "paylocity", f"{m.group(1)}|{m.group(2)}"

    # UKG (Ultipro/Pro)
    m = re.search(r"([A-Za-z0-9_.-]+\.(?:ultipro|ukg)\.com)/JobBoard/([A-Za-z0-9-]+)/JobBoardView/([A-Za-z0-9-]+)", body + " " + final_url)
    if m:
        return "ukg", f"{m.group(1)}|{m.group(2)}|{m.group(3)}"

    # Oracle HCM / Fusion / ORC
    m = re.search(r"([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#\"'\s]+)", body + " " + final_url)
    if m:
        return "oracle_hcm", f"{m.group(1)}|{m.group(2)}"

    # Eightfold — many enterprises. Two forms: {slug}.eightfold.ai OR corporate host with vendor CSS
    m = re.search(r"([a-z0-9-]+)\.eightfold\.ai", body + " " + final_url)
    if m:
        slug = m.group(1)
        # Best-effort domain: pull from body if present, else default to slug.com
        dm = re.search(r'"domain"\s*:\s*"([^"]+)"', body)
        domain = dm.group(1) if dm else f"{slug}.com"
        return "eightfold", f"{slug}|{domain}"
    # Fallback: many corp hosts serve Eightfold; the CSS fingerprint is stable.
    if "eightfold-font-base.css" in body or "static.vscdn.net/images/eightfold" in body:
        # Best-effort: derive slug from host
        slug_guess = host.split(".")[0] if host else ""
        return "eightfold", f"{slug_guess}|{slug_guess}.com"

    # Phenom People — corporate host but XHRs to /widgets or /api/jobs/search
    if "phenompeople.com" in body or "phenomcloud" in body or "/widgets?" in body:
        return "phenom", f"https://{host}"

    # SAP SuccessFactors
    m = re.search(r"career4\.successfactors\.com/career\?company=([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "successfactors", m.group(1)
    if "jobs.hr.cloud.sap" in host or "jobs.hr.cloud.sap" in body:
        return "successfactors", f"|{host or 'jobs.hr.cloud.sap'}"

    # Oracle Taleo
    m = re.search(r"([A-Za-z0-9_-]+)\.taleo\.net/careersection/(\d+)", body + " " + final_url)
    if m:
        return "taleo", f"{m.group(1)}|{m.group(2)}"

    # JazzHR
    m = re.search(r"([A-Za-z0-9_-]+)\.applytojob\.com", body + " " + final_url)
    if m:
        return "jazzhr", m.group(1)

    # Gem
    m = re.search(r"jobs\.gem\.com/([A-Za-z0-9_-]+)", body + " " + final_url)
    if m:
        return "gem", m.group(1)

    # ---- Body-content markers (weakest tier, catches vendor hints without URL) ----
    if "workday.com" in body and "myworkdayjobs" not in body:
        return None, None  # incomplete workday hint — need the full tenant path

    return None, None


if __name__ == "__main__":
    # Ad-hoc CLI: python -m app.utils.ats_sniff URL [URL ...]
    import sys
    for u in sys.argv[1:]:
        ats, seed = sniff(u)
        print(f"{u}  ->  ats={ats}  seed={seed}")
