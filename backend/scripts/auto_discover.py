"""
scripts/auto_discover.py
------------------------
Grow the company list automatically. Without this the crawler pulls new JOBS
24/7 but never finds new COMPANIES: the roster was seeded once (21,606 rows on
2026-06-17) and then sat frozen for over a month, because every discovery script
in this repo had to be run by hand and nothing ever called them.

Strategy: most companies run their board on a token derived from their own name,
and many run boards on MORE than one ATS. So for every company already known, we
generate candidate tokens from its name and probe the public, keyless list APIs
of the token-based platforms. A board that answers with >0 live postings is real
and gets seeded.

Only ATSes with a public token API can be discovered this way. Paylocity, UKG
and Oracle HCM are deliberately excluded: their boards are keyed by GUIDs and
per-tenant hostnames that cannot be derived from a company name, so probing them
would be pure waste. Workable is also excluded: apply.workable.com is now behind
a Cloudflare managed challenge (see crawlers/workable.py), so any board found
here can't actually be crawled -- probing it 23k names/week is wasted traffic.

    python scripts/auto_discover.py --dry-run       # report, write nothing
    python scripts/auto_discover.py                 # one pass
    python scripts/auto_discover.py --loop --every-hours 168   # weekly forever

Safe to run alongside the live crawler: probing is pure HTTP, and the only DB
writes are INSERTs of brand-new companies, committed in small batches so the
single SQLite writer is never held for long.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select  # noqa: E402

from app.database import session_scope  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.utils.logging import get_logger  # noqa: E402

log = get_logger("auto_discover")

HEADERS = {"User-Agent": "JobControlCenter/1.0 (+personal-job-search; respectful)"}
TIMEOUT = 12

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))


# --- Probes: each returns a live posting count, or None if it isn't a board. ---
# Note on SmartRecruiters: a bogus token and a real-but-empty board BOTH return
# HTTP 200 with totalFound 0, so >0 postings is the only trustworthy signal.
def _smartrecruiters(tok: str):
    r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=1",
                    timeout=TIMEOUT)
    return (r.json() or {}).get("totalFound", 0) if r.status_code == 200 else None


def _greenhouse(tok: str):
    r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs", timeout=TIMEOUT)
    return len(r.json().get("jobs", []) or []) if r.status_code == 200 else None


def _lever(tok: str):
    r = SESSION.get(f"https://api.lever.co/v0/postings/{tok}?mode=json", timeout=TIMEOUT)
    d = r.json() if r.status_code == 200 else None
    return len(d) if isinstance(d, list) else None


def _ashby(tok: str):
    r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{tok}", timeout=TIMEOUT)
    return len(r.json().get("jobs", []) or []) if r.status_code == 200 else None


def _recruitee(tok: str):
    r = SESSION.get(f"https://{tok}.recruitee.com/api/offers/", timeout=TIMEOUT)
    return len(r.json().get("offers", []) or []) if r.status_code == 200 else None


# BambooHR is the single biggest ATS bucket in the roster (~6.8k companies) yet
# was never probed here, so it never grew automatically. Its public careers list
# is keyless and token-derivable, exactly like the others.
def _bamboohr(tok: str):
    r = SESSION.get(f"https://{tok}.bamboohr.com/careers/list", timeout=TIMEOUT)
    return len(r.json().get("result", []) or []) if r.status_code == 200 else None


def _teamtailor(tok: str):
    r = SESSION.get(f"https://{tok}.teamtailor.com/jobs.json", timeout=TIMEOUT)
    return len(r.json().get("items", []) or []) if r.status_code == 200 else None


def _pinpoint(tok: str):
    r = SESSION.get(f"https://{tok}.pinpointhq.com/postings.json", timeout=TIMEOUT)
    return len(r.json().get("data", []) or []) if r.status_code == 200 else None


# Personio's feed is XML, not JSON: count <position> elements without a parser.
def _personio(tok: str):
    r = SESSION.get(f"https://{tok}.jobs.personio.com/xml?language=en", timeout=TIMEOUT)
    return r.text.count("<position>") if r.status_code == 200 else None


PROBES = {
    "smartrecruiters": _smartrecruiters,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "recruitee": _recruitee,
    "bamboohr": _bamboohr,
    "teamtailor": _teamtailor,
    "pinpoint": _pinpoint,
    "personio": _personio,
}


# ---- HTML-based ATS fingerprinting -------------------------------------------
# When a seed carries a known website (hiring-without-whiteboards does), we can
# skip the token-guessing lottery and READ the ATS off the company's own
# careers page. This catches Fortune 500 tenants whose slugs aren't derivable
# from company name (e.g. "jpmc.wd5.myworkdayjobs.com" for "JPMorgan Chase").

# Path candidates -- careers pages live under one of these on ~95% of sites.
CAREERS_PATHS = (
    "/careers", "/jobs", "/careers/", "/jobs/",
    "/company/careers", "/about/careers", "/join-us", "/join", "/work-with-us",
    "/openings", "/opportunities",
)

# Fingerprint patterns: (ats_name, regex against HTML content). The regex must
# capture the tenant/token in group 1 so we can seed a real Company row.
_FINGERPRINTS: list[tuple[str, re.Pattern]] = [
    # Greenhouse: <script src="//boards.greenhouse.io/embed/job_board?for=TOKEN">
    ("greenhouse", re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=|v1/boards/)([a-z0-9._-]+)", re.I)),
    # Lever: iframe/link to jobs.lever.co/TOKEN or api.lever.co/v0/postings/TOKEN
    ("lever", re.compile(r"(?:jobs\.lever\.co/|api\.lever\.co/v0/postings/)([a-z0-9._-]+)", re.I)),
    # Ashby: jobs.ashbyhq.com/TOKEN or api.ashbyhq.com/posting-api/job-board/TOKEN
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com/|posting-api/job-board/)([a-z0-9._-]+)", re.I)),
    # SmartRecruiters: careers.smartrecruiters.com/TOKEN or api.smartrecruiters.com/v1/companies/TOKEN
    ("smartrecruiters", re.compile(r"(?:careers\.smartrecruiters\.com/|/v1/companies/)([A-Za-z0-9._-]+)", re.I)),
    # Workday: TENANT.wd##.myworkdayjobs.com/en-US/SITE  -> we return "tenant|dc|site"
    ("workday", re.compile(r"([a-z0-9]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
    # iCIMS: TOKEN.icims.com
    ("icims", re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    # BambooHR
    ("bamboohr", re.compile(r"([a-z0-9-]+)\.bamboohr\.com/(?:jobs|careers)", re.I)),
    # Teamtailor
    ("teamtailor", re.compile(r"([a-z0-9.-]+)\.teamtailor\.com", re.I)),
    # Personio
    ("personio", re.compile(r"([a-z0-9-]+)\.jobs\.personio\.com", re.I)),
    # Pinpoint
    ("pinpoint", re.compile(r"([a-z0-9-]+)\.pinpointhq\.com", re.I)),
    # Recruitee
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    # Eightfold (Fortune 500 usage)
    ("eightfold", re.compile(r"([a-z0-9-]+)\.eightfold\.ai", re.I)),
]


def _from_url(url: str) -> Optional[tuple[str, str]]:
    """Direct-hit case: the seed URL already IS an ATS URL (e.g.
    hiring-without-whiteboards links straight to 'ats.rippling.com/foo/jobs'
    for some entries). Match against fingerprint regexes on the URL itself
    before we bother with an HTTP fetch."""
    for ats, pat in _FINGERPRINTS:
        m = pat.search(url)
        if not m:
            continue
        if ats == "workday":
            return ats, f"{m.group(1)}|{m.group(2)}|{m.group(3)}"
        token = m.group(1).lower()
        if token in {"www", "jobs", "boards", "api", "careers", "static"}:
            continue
        return ats, token
    return None


def fingerprint_ats(website: str) -> Optional[tuple[str, str]]:
    """Fetch the company's careers page and try to detect its ATS.

    Returns (ats_type, career_url) on success, or None. `career_url` is the
    token/tenant the platform-specific crawlers expect. Workday's return is a
    "tenant|dc|site" pipe string, matching how workday.py already parses it.

    Fast path: if the URL itself embeds an ATS host (e.g. the seed links
    directly to ats.rippling.com/foo), skip the HTTP fetch entirely.

    Slow path: fetch the URL + conventional careers paths and pattern-match
    the HTML body for embedded ATS scripts/iframes. HTTP failures / TLS
    timeouts return None silently (a bad careers site can't kill the pass).
    """
    if not website:
        return None
    direct = _from_url(website)
    if direct:
        return direct

    urls = [website]
    try:
        p = urlparse(website)
        root = f"{p.scheme}://{p.netloc}"
        # Reject noise domains before they ever hit HTTP.
        if any(bd in p.netloc.lower() for bd in
               ("news.ycombinator.com", "twitter.com", "x.com",
                "medium.com", "youtube.com", "wikipedia.org", "reddit.com",
                "github.com/", "web.archive.org")):
            return None
        for path in CAREERS_PATHS:
            urls.append(root + path)
    except Exception:
        pass
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code >= 400:
                continue
            html = resp.text[:200_000]
        except Exception:
            continue
        for ats, pat in _FINGERPRINTS:
            m = pat.search(html)
            if not m:
                continue
            if ats == "workday":
                return ats, f"{m.group(1)}|{m.group(2)}|{m.group(3)}"
            token = m.group(1).lower()
            if token in {"www", "jobs", "boards", "api", "careers", "static"}:
                continue
            return ats, token
    return None


def load_seeds(path: str) -> list[dict]:
    """Read the JSONL produced by harvest_company_sources.py."""
    out: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except FileNotFoundError:
        log.warning("seed file not found: %s", path)
    return out


def candidates(name: str) -> list[str]:
    """Token forms these ATSes actually use. Lowercase only — every probed API
    is case-insensitive, so probing both cases would just double the traffic."""
    base = re.sub(r"[^A-Za-z0-9 ]+", "", name or "").strip()
    if not base:
        return []
    squashed = base.replace(" ", "").lower()
    hyphen = base.replace(" ", "-").lower()
    out = [squashed] + ([hyphen] if hyphen != squashed else [])
    return [t for t in out if len(t) >= 3]


def one_pass(dry_run: bool = False, workers: int = 16, limit: int = 0,
             seed_file: Optional[str] = None) -> dict:
    with session_scope() as s:
        companies = s.exec(select(Company)).all()
        # (ats, token) pairs we already have — never re-probe or re-insert these.
        known = {(c.ats_type or "", (c.career_url or "").strip().lower()) for c in companies}
        # Names already in the roster (case-insensitive, alnum-only) so we don't
        # re-add "Airbnb" from seeds when it's already there under any ATS.
        known_names = {re.sub(r"[^a-z0-9]+", "", (c.name or "").lower())
                       for c in companies}
        names = [(c.name, c.h1b_history_score or 0, c.priority or "medium")
                 for c in companies]

    # Ingest new seeds: name+website pairs from harvest_company_sources.py.
    # New-to-us names are appended for the token-probe pass; if the seed has
    # a website, we ALSO try HTML fingerprinting -- that catches Fortune 500
    # tenants whose slugs can't be guessed from the name.
    fp_hits: list[tuple[str, str, str, int, int, str]] = []
    if seed_file:
        seeds = load_seeds(seed_file)
        log.info("seed file: %d entries loaded from %s", len(seeds), seed_file)
        n_new_names = 0
        n_fp_scanned = 0
        websites = []
        for seed in seeds:
            name = (seed.get("name") or "").strip()
            website = (seed.get("website") or "").strip()
            if not name:
                continue
            slug = re.sub(r"[^a-z0-9]+", "", name.lower())
            if slug and slug not in known_names:
                names.append((name, 0, "low"))  # seeds default to low priority
                known_names.add(slug)
                n_new_names += 1
            if website:
                websites.append((name, website))
        log.info("seeds: +%d new names for token probing, %d websites to fingerprint",
                 n_new_names, len(websites))

        # HTML fingerprinting pass (fully in parallel).
        def fp_one(item):
            name, website = item
            r = fingerprint_ats(website)
            if not r:
                return None
            ats, career_url = r
            key = (ats, career_url.lower())
            if key in known:
                return None
            return (name, career_url, ats, 1, 0, "low")

        with ThreadPoolExecutor(workers) as ex:
            for res in ex.map(fp_one, websites):
                n_fp_scanned += 1
                if res:
                    fp_hits.append(res)
                if n_fp_scanned % 100 == 0:
                    log.info("  fingerprint scan: %d/%d, %d ATSes detected",
                             n_fp_scanned, len(websites), len(fp_hits))
        log.info("fingerprint pass: scanned %d websites, detected %d new ATS boards",
                 n_fp_scanned, len(fp_hits))

    if limit:
        names = names[:limit]
    # Sponsors first: if a pass is cut short, the highest-value boards are done.
    names.sort(key=lambda t: -t[1])
    log.info("auto-discovery: probing %d companies across %d platforms",
             len(names), len(PROBES))

    def probe_company(item):
        name, score, prio = item
        hits = []
        for tok in candidates(name):
            for ats, fn in PROBES.items():
                if (ats, tok) in known:
                    continue
                try:
                    n = fn(tok)
                except Exception:
                    continue
                if n and n > 0:
                    hits.append((name, tok, ats, n, score, prio))
        return hits

    found, done = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for hits in ex.map(probe_company, names):
            done += 1
            if done % 2000 == 0:
                log.info("  ...%d/%d probed, %d boards found", done, len(names), len(found))
            found.extend(hits)

    # Fold in fingerprint hits from the seed pass (Fortune-500-safe tenants
    # we could never have guessed from the company name alone).
    found.extend(fp_hits)

    # Dedupe within this run: the same (ats, token) can be reached from two
    # differently-named rows for the same employer.
    uniq = {}
    for name, tok, ats, n, score, prio in found:
        uniq.setdefault((ats, tok), (name, tok, ats, n, score, prio))

    summary = {"probed": len(names), "boards": len(uniq),
               "postings": sum(v[3] for v in uniq.values()), "added": 0}
    if dry_run:
        log.info("DRY RUN: %d new boards, %d live postings (nothing written)",
                 summary["boards"], summary["postings"])
        for v in sorted(uniq.values(), key=lambda v: -v[3])[:15]:
            log.info("   %-28s %-16s %-14s %5d postings", v[0][:28], v[1][:16], v[2], v[3])
        return summary

    batch = []
    with session_scope() as s:
        for name, tok, ats, n, score, prio in uniq.values():
            batch.append(Company(
                name=name, career_url=tok, ats_type=ats,
                h1b_history_score=score, priority=prio, is_active=True,
                notes=f"auto-discovered {datetime.utcnow():%Y-%m-%d}; {n} live postings"))
            if len(batch) >= 200:
                s.add_all(batch); s.commit(); summary["added"] += len(batch); batch = []
        if batch:
            s.add_all(batch); s.commit(); summary["added"] += len(batch)
    log.info("auto-discovery: added %d companies (%d live postings behind them)",
             summary["added"], summary["postings"])
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="run forever on an interval")
    ap.add_argument("--every-hours", type=float, default=168.0, help="loop interval (default weekly)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N companies")
    ap.add_argument("--seed-file", default=None,
                    help="also ingest company name+website pairs from a JSONL "
                         "produced by scripts/harvest_company_sources.py. Websites "
                         "get HTML-based ATS fingerprinting; new names get token "
                         "probing like existing roster.")
    args = ap.parse_args()

    while True:
        try:
            one_pass(dry_run=args.dry_run, workers=args.workers, limit=args.limit,
                     seed_file=args.seed_file)
        except Exception as exc:  # noqa: BLE001 - a failed pass must not kill the loop
            log.warning("discovery pass failed: %s", exc)
        if not args.loop:
            return 0
        log.info("next discovery pass in %.1fh", args.every_hours)
        time.sleep(max(600, args.every_hours * 3600))


if __name__ == "__main__":
    sys.exit(main())
